import logging
import os
import shutil
import time
import uuid
from typing import cast, Dict, List, Optional, Tuple, Type, Union

import boto3
import docker

from sebs.aws.s3 import S3
from sebs.aws.function import LambdaFunction
from sebs.aws.config import AWSConfig
from sebs import utils
from sebs.benchmark import Benchmark
from sebs.cache import Cache
from sebs.config import SeBSConfig
from ..faas.function import Function, ExecutionResult
from ..faas.storage import PersistentStorage
from ..faas.system import System


class AWS(System):
    logs_client = None
    storage: S3
    cached = False
    _config: AWSConfig

    @staticmethod
    def name():
        return "aws"

    @staticmethod
    def function_type() -> "Type[Function]":
        return LambdaFunction

    @property
    def config(self) -> AWSConfig:
        return self._config

    """
        :param cache_client: Function cache instance
        :param config: Experiments config
        :param docker_client: Docker instance
    """

    def __init__(
        self,
        sebs_config: SeBSConfig,
        config: AWSConfig,
        cache_client: Cache,
        docker_client: docker.client,
    ):
        super().__init__(sebs_config, cache_client, docker_client)
        self._config = config

    def initialize(self, config: Dict[str, str] = {}):
        # thread-safe
        self.session = boto3.session.Session()
        self.get_lambda_client()
        self.get_storage()
        self.get_events_client()

    def get_lambda_client(self):
        if not hasattr(self, "client"):
            self.client = self.session.client(
                service_name="lambda",
                aws_access_key_id=self.config.credentials.access_key,
                aws_secret_access_key=self.config.credentials.secret_key,
                region_name=self.config.region,
            )
        return self.client

    """
        Create a client instance for cloud storage. When benchmark and buckets
        parameters are passed, then storage is initialized with required number
        of buckets. Buckets may be created or retrieved from cache.

        :param benchmark: benchmark name
        :param buckets: tuple of required input/output buckets
        :param replace_existing: replace existing files in cached buckets?
        :return: storage client
    """

    def get_storage(self, replace_existing: bool = False) -> PersistentStorage:
        if not hasattr(self, "storage"):
            self.storage = S3(
                self.session,
                self.cache_client,
                self.config.region,
                access_key=self.config.credentials.access_key,
                secret_key=self.config.credentials.secret_key,
                replace_existing=replace_existing,
            )
        else:
            self.storage.replace_existing = replace_existing
        return self.storage

    """
        Create a client instance for Event Bridge
        
        :return: events client
    """


    def get_events_client(self):
        if not hasattr(self, "events_client"):
            self.events_client = self.session.client(
                service_name="events",
                aws_access_key_id=self.config.credentials.access_key,
                aws_secret_access_key=self.config.credentials.secret_key,
                region_name=self.config.region,
            )
        return self.events_client

    """
        Create a client instance for Dynamo DB

        :return: db client
    """

    def get_db_client(self):
        if not hasattr(self, "db_client"):
            self.db_client = self.session.client(
                service_name="dynamodb",
                aws_access_key_id=self.config.credentials.access_key,
                aws_secret_access_key=self.config.credentials.secret_key,
                region_name=self.config.region,
            )
        return self.db_client

    """
        It would be sufficient to just pack the code and ship it as zip to AWS.
        However, to have a compatible function implementation across providers,
        we create a small module.
        Issue: relative imports in Python when using storage wrapper.
        Azure expects a relative import inside a module thus it's easier
        to always create a module.

        Structure:
        function
        - function.py
        - storage.py
        - resources
        handler.py

        benchmark: benchmark name
    """

    def package_code(
        self, directory: str, language_name: str, benchmark: str
    ) -> Tuple[str, int]:

        CONFIG_FILES = {
            "python": ["handler.py", "requirements.txt", ".python_packages"],
            "nodejs": ["handler.js", "package.json", "node_modules"],
        }
        package_config = CONFIG_FILES[language_name]
        function_dir = os.path.join(directory, "function")
        os.makedirs(function_dir)
        # move all files to 'function' except handler.py
        for file in os.listdir(directory):
            if file not in package_config:
                file = os.path.join(directory, file)
                shutil.move(file, function_dir)

        # FIXME: use zipfile
        # create zip with hidden directory but without parent directory
        utils.execute(
            "zip -qu -r9 {}.zip * .".format(benchmark), shell=True, cwd=directory
        )
        benchmark_archive = "{}.zip".format(os.path.join(directory, benchmark))
        logging.info("AWS: Created {} archive".format(benchmark_archive))

        bytes_size = os.path.getsize(os.path.join(directory, benchmark_archive))
        mbytes = bytes_size / 1024.0 / 1024.0
        logging.info("AWS: Zip archive size {:2f} MB".format(mbytes))

        return os.path.join(directory, "{}.zip".format(benchmark)), bytes_size

    def create_lambda_function(
        self,
        benchmark: Benchmark,
        function_name: str,
        api_id: str,
        parent_id: str,
        package: str,
        code_size: int,
        memory: int,
        timeout: int,
        experiment_config: dict,
    ):
        language = benchmark.language_name
        language_runtime = benchmark.language_version
        logging.info("Creating function {} from {}".format(function_name, package))

        # TODO: create Lambda role
        # AWS Lambda limit on zip deployment size
        # Limit to 50 MB
        # mypy doesn't recognize correctly the case when the same
        # variable has different types across the path
        code_config: Dict[str, Union[str, bytes]]
        if code_size < 50 * 1024 * 1024:
            package_body = open(package, "rb").read()
            code_config = {"ZipFile": package_body}
        # Upload code package to S3, then use it
        else:
            code_package_name = cast(str, os.path.basename(package))
            bucket, idx = self.storage.add_input_bucket(function_name)
            self.storage.upload(bucket, package, code_package_name)
            logging.info(
                "Uploading function {} code to {}".format(function_name, bucket)
            )
            code_config = {"S3Bucket": bucket, "S3Key": code_package_name}
        self.client.create_function(
            FunctionName=function_name,
            Runtime="{}{}".format(language, language_runtime),
            Handler="handler.handler",
            Role=self.config.resources.lambda_role,
            MemorySize=memory,
            Timeout=timeout,
            Code=code_config,
        )
        while True:
            try:
                logging.info(
                    "Creating HTTP Trigger for function {} from {}".format(
                        function_name, package
                    )
                )
                url = self.create_http_trigger(function_name, api_id, parent_id)
                logging.info(url)
            except Exception as e:
                logging.info("Exception")
                logging.info(e)
                import traceback

                traceback.print_exc()
                api_client = boto3.client(
                    service_name="apigateway",
                    aws_access_key_id=self.config.credentials.access_key,
                    aws_secret_access_key=self.config.credentials.secret_key,
                    region_name=self.config.region,
                )
                resp = api_client.get_resources(restApiId=api_id)["items"]
                for v in resp:
                    if "pathPart" in v:
                        path = v["pathPart"]
                        if path == function_name:
                            resource_id = v["id"]
                            logging.info(
                                "Remove resource with path {} from {}".format(
                                    function_name, api_id
                                )
                            )
                            api_client.delete_resource(
                                restApiId=api_id, resourceId=resource_id
                            )
                            break
                # throttling on AWS
                continue
            logging.info("Done")
            break
        logging.info(
            "Created HTTP Trigger for function {} from {}".format(
                function_name, package
            )
        )
        return url

    def create_function(
        self, code_package: Benchmark, func_name: str
    ) -> "LambdaFunction":

        package = code_package.code_location
        benchmark = code_package.benchmark
        language = code_package.language_name
        language_runtime = code_package.language_version
        timeout = code_package.benchmark_config.timeout
        memory = code_package.benchmark_config.memory
        code_size = code_package.code_size
        code_bucket: Optional[str] = None
        trigger_config = code_package.benchmark_config.trigger_config

        # we can either check for exception or use list_functions
        # there's no API for test
        try:
            function_response = self.client.get_function(FunctionName=func_name)
            logging.info(
                "Function {} exists on AWS, retrieve configuration.".format(func_name)
            )
            # Here we assume a single Lambda role
            lambda_function = LambdaFunction(
                func_name,
                code_package.hash,
                timeout,
                memory,
                language_runtime,
                self.config.resources.lambda_role,
                None,
                function_response['Configuration']['FunctionArn']
            )
            self.update_function(lambda_function, code_package)
            lambda_function.updated_code = True
            # TODO: get configuration of REST API
            # url = None
        except self.client.exceptions.ResourceNotFoundException:
            logging.info("Creating function {} from {}".format(func_name, package))

            # TODO: create Lambda role
            # AWS Lambda limit on zip deployment size
            # Limit to 50 MB
            # mypy doesn't recognize correctly the case when the same
            # variable has different types across the path
            code_config: Dict[str, Union[str, bytes]]
            if code_size < 50 * 1024 * 1024:
                package_body = open(package, "rb").read()
                code_config = {"ZipFile": package_body}
            # Upload code package to S3, then use it
            else:
                code_package_name = cast(str, os.path.basename(package))
                code_bucket, idx = self.storage.add_input_bucket(benchmark)
                self.storage.upload(code_bucket, package, code_package_name)
                logging.info(
                    "Uploading function {} code to {}".format(func_name, code_bucket)
                )
                code_config = {"S3Bucket": code_bucket, "S3Key": code_package_name}
            function_response = self.client.create_function(
                FunctionName=func_name,
                Runtime="{}{}".format(language, language_runtime),
                Handler="handler.handler",
                Role=self.config.resources.lambda_role,
                MemorySize=memory,
                Timeout=timeout,
                Code=code_config,
            )
            # url = self.create_http_trigger(func_name, None, None)
            # print(url)
            lambda_function = LambdaFunction(
                func_name,
                code_package.hash,
                timeout,
                memory,
                language_runtime,
                self.config.resources.lambda_role,
                code_bucket,
                function_response['FunctionArn']
            )

        from sebs.aws.triggers import Trigger, LibraryTrigger, StorageTrigger, TimerTrigger, DbTrigger

        if trigger_config.type == Trigger.TriggerType.LIBRARY:
            trigger = LibraryTrigger(func_name, self)
        elif trigger_config.type == Trigger.TriggerType.STORAGE:
            if "bucketName" in trigger_config.params:
                bucket_name = trigger_config.params["bucketName"]
                trigger = StorageTrigger(lambda_function.arn, bucket_name, self)
            else:
                unique_bucket_name = '{}{}'.format(''.join(filter(str.isalnum, func_name)), time.time())
                trigger = StorageTrigger(lambda_function.arn, unique_bucket_name, self)
            trigger.create()
        elif trigger_config.type == Trigger.TriggerType.TIMER:
            schedule_pattern = trigger_config.params["pattern"]
            name = None if "ruleName" not in trigger_config.params else trigger_config.params["ruleName"]
            trigger = TimerTrigger(func_name, lambda_function.arn, schedule_pattern, self, name)
            trigger.create()
        elif trigger_config.type == Trigger.TriggerType.DB:
            trigger = DbTrigger(lambda_function.arn, trigger_config.params, self)
            trigger.create()
        lambda_function.add_trigger(trigger)
        return lambda_function

    def cached_function(self, function: Function):

        from sebs.aws.triggers import LibraryTrigger

        for trigger in function.triggers:
            if isinstance(trigger, LibraryTrigger):
                trigger.deployment_client = self

    """
        Update function code and configuration on AWS.

        :param benchmark: benchmark name
        :param name: function name
        :param code_package: path to code package
        :param code_size: size of code package in bytes
        :param timeout: function timeout in seconds
        :param memory: memory limit for function
    """

    def update_function(self, function: Function, code_package: Benchmark):

        function = cast(LambdaFunction, function)
        name = function.name
        code_size = code_package.code_size
        package = code_package.code_location
        # Run AWS update
        # AWS Lambda limit on zip deployment
        if code_size < 50 * 1024 * 1024:
            with open(package, "rb") as code_body:
                self.client.update_function_code(
                    FunctionName=name, ZipFile=code_body.read()
                )
        # Upload code package to S3, then update
        else:
            code_package_name = os.path.basename(package)
            bucket = function.code_bucket(code_package.benchmark, self.storage)
            self.storage.upload(bucket, package, code_package_name)
            self.client.update_function_code(
                FunctionName=name, S3Bucket=bucket, S3Key=code_package_name
            )
        # and update config
        self.client.update_function_configuration(
            FunctionName=name, Timeout=function.timeout, MemorySize=function.memory
        )

    @staticmethod
    def default_function_name(code_package: Benchmark) -> str:
        # Create function name
        func_name = "{}-{}-{}".format(
            code_package.benchmark,
            code_package.language_name,
            code_package.benchmark_config.memory,
        )
        # AWS Lambda does not allow hyphens in function names
        func_name = func_name.replace("-", "_")
        func_name = func_name.replace(".", "_")
        return func_name

    """
        FIXME: does not clean the cache
    """

    def delete_function(self, func_name: Optional[str]):
        logging.info("Deleting function {}".format(func_name))
        try:
            self.client.delete_function(FunctionName=func_name)
        except Exception:
            logging.info("Function {} does not exist!".format(func_name))

    def create_http_trigger(
        self, func_name: str, api_id: Optional[str], parent_id: Optional[str]
    ):

        # https://github.com/boto/boto3/issues/572
        # assumed we have: function name, region

        api_client = boto3.client(
            service_name="apigateway",
            aws_access_key_id=self.config.credentials.access_key,
            aws_secret_access_key=self.config.credentials.secret_key,
            region_name=self.config.region,
        )

        # create REST API
        if api_id is None:
            api_name = func_name
            api = api_client.create_rest_api(name=api_name)
            api_id = api["id"]
        if parent_id is None:
            resources = api_client.get_resources(restApiId=api_id)
            for r in resources["items"]:
                if r["path"] == "/":
                    parent_id = r["id"]

        # create resource
        # TODO: check if resource exists
        resource_id = None
        resp = api_client.get_resources(restApiId=api_id)["items"]
        for v in resp:
            if "pathPart" in v:
                path = v["pathPart"]
                if path == func_name:
                    resource_id = v["id"]
                    break
        if not resource_id:
            logging.info(func_name)
            logging.info(parent_id)
            resource = api_client.create_resource(
                restApiId=api_id, parentId=cast(str, parent_id), pathPart=func_name
            )
            logging.info(resource)
            resource_id = resource["id"]
        logging.info(
            "AWS: using REST API {api_id} with parent ID {parent_id}"
            "using resource ID {resource_id}".format(
                api_id=api_id, parent_id=parent_id, resource_id=resource_id
            )
        )

        # create POST method
        api_client.put_method(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="POST",
            authorizationType="NONE",
            apiKeyRequired=False,
        )

        lambda_version = self.client.meta.service_model.api_version
        # get account information
        sts_client = boto3.client(
            service_name="sts",
            aws_access_key_id=self.config.credentials.access_key,
            aws_secret_access_key=self.config.credentials.secret_key,
            region_name=self.config.region,
        )
        account_id = sts_client.get_caller_identity()["Account"]

        uri_data = {
            "aws-region": self.config.resources.lambda_role,
            "api-version": lambda_version,
            "aws-acct-id": account_id,
            "lambda-function-name": func_name,
        }

        uri = (
            "arn:aws:apigateway:{aws-region}:lambda:path/{api-version}/"
            "functions/arn:aws:lambda:{aws-region}:{aws-acct-id}:function"
            ":{lambda-function-name}/invocations"
        ).format(**uri_data)

        # create integration
        api_client.put_integration(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="POST",
            type="AWS",
            integrationHttpMethod="POST",
            uri=uri,
        )

        api_client.put_integration_response(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="POST",
            statusCode="200",
            selectionPattern=".*",
        )

        # create POST method response
        api_client.put_method_response(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="POST",
            statusCode="200",
        )

        uri_data["aws-api-id"] = api_id
        source_arn = (
            "arn:aws:execute-api:{aws-region}:{aws-acct-id}:{aws-api-id}/*/"
            "POST/{lambda-function-name}"
        ).format(**uri_data)

        self.client.add_permission(
            FunctionName=func_name,
            StatementId=uuid.uuid4().hex,
            Action="lambda:InvokeFunction",
            Principal="apigateway.amazonaws.com",
            SourceArn=source_arn,
        )

        # state 'your stage name' was already created via API Gateway GUI
        stage_name = "name"
        api_client.create_deployment(restApiId=api_id, stageName=stage_name)
        uri_data["api_id"] = api_id
        uri_data["stage_name"] = stage_name
        url = (
            "https://{api_id}.execute-api.{aws-region}.amazonaws.com/"
            "{stage_name}/{lambda-function-name}"
        )
        return url.format(**uri_data)

    """
        Prepare AWS resources to store experiment results.
        Allocate one bucket.

        :param benchmark: benchmark name
        :return: name of bucket to store experiment results
    """

    def prepare_experiment(self, benchmark: str):
        logs_bucket = self.storage.add_output_bucket(benchmark, suffix="logs")
        return logs_bucket

    """
        Accepts AWS report after function invocation.
        Returns a dictionary filled with values with various metrics such as
        time, invocation time and memory consumed.

        :param log: decoded log from CloudWatch or from synchronuous invocation
        :return: dictionary with parsed values
    """

    @staticmethod
    def parse_aws_report(log: str, output: ExecutionResult):
        aws_vals = {}
        for line in log.split("\t"):
            if not line.isspace():
                split = line.split(":")
                aws_vals[split[0]] = split[1].split()[0]
        output.request_id = aws_vals["START RequestId"]
        output.times.provider = int(float(aws_vals["Duration"]) * 1000)
        output.stats.memory_used = float(aws_vals["Max Memory Used"])
        if "Init Duration" in aws_vals:
            output.stats.init_time_reported = int(
                float(aws_vals["Init Duration"]) * 1000
            )
        output.billing.billed_time = int(aws_vals["Billed Duration"])
        output.billing.memory = int(aws_vals["Memory Size"])
        output.billing.gb_seconds = output.billing.billed_time * output.billing.memory

    def shutdown(self) -> None:
        pass

    def get_invocation_error(self, function_name: str, start_time: int, end_time: int):
        if not self.logs_client:
            self.logs_client = boto3.client(
                service_name="logs",
                aws_access_key_id=self.config.credentials.access_key,
                aws_secret_access_key=self.config.credentials.secret_key,
                region_name=self.config.region,
            )

        response = None
        while True:
            query = self.logs_client.start_query(
                logGroupName="/aws/lambda/{}".format(function_name),
                queryString="filter @message like /REPORT/",
                startTime=start_time,
                endTime=end_time,
            )
            query_id = query["queryId"]

            while response is None or response["status"] == "Running":
                logging.info("Waiting for AWS query to complete ...")
                time.sleep(1)
                response = self.logs_client.get_query_results(queryId=query_id)
            if len(response["results"]) == 0:
                logging.info("AWS logs are not yet available, repeat ...")
                response = None
                break
            else:
                break
        print(response)

    def download_metrics(
        self,
        function_name: str,
        deployment_config: dict,
        start_time: int,
        end_time: int,
        requests: dict,
    ):

        if not self.logs_client:
            self.logs_client = boto3.client(
                service_name="logs",
                aws_access_key_id=self.config.credentials.access_key,
                aws_secret_access_key=self.config.credentials.secret_key,
                region_name=self.config.region,
            )

        query = self.logs_client.start_query(
            logGroupName="/aws/lambda/{}".format(function_name),
            queryString="filter @message like /REPORT/",
            startTime=start_time,
            endTime=end_time,
        )
        query_id = query["queryId"]
        response = None

        while response is None or response["status"] == "Running":
            logging.info("Waiting for AWS query to complete ...")
            time.sleep(1)
            response = self.logs_client.get_query_results(queryId=query_id)
        # results contain a list of matches
        # each match has multiple parts, we look at `@message` since this one
        # contains the report of invocation
        results = response["results"]
        for val in results:
            for result_part in val:
                if result_part["field"] == "@message":
                    actual_result = AWS.parse_aws_report(result_part["value"])
                    request_id = actual_result["REPORT RequestId"]
                    if request_id not in requests:
                        logging.info(
                            "Found invocation {} without result in bucket!".format(
                                request_id
                            )
                        )
                    del actual_result["REPORT RequestId"]
                    requests[request_id][self.name()] = actual_result

    def create_function_copies(
        self,
        benchmark: Benchmark,
        function_names: List[str],
        api_name: str,
        memory: int,
        timeout: int,
        code_package: Benchmark,
        experiment_config: dict,
        api_id: str = None,
    ):

        code_location = code_package.code_location
        code_size = code_package.code_size
        timeout = code_package.benchmark_config.timeout
        memory = code_package.benchmark_config.memory

        self.get_lambda_client()
        api_client = boto3.client(
            service_name="apigateway",
            aws_access_key_id=self.config.credentials.access_key,
            aws_secret_access_key=self.config.credentials.secret_key,
            region_name=self.config.region,
        )
        # api_name = '{api_name}_API'.format(api_name=api_name)
        if api_id is None:
            api = api_client.create_rest_api(name=api_name)
            api_id = api["id"]
        resource = api_client.get_resources(restApiId=api_id)
        for r in resource["items"]:
            if r["path"] == "/":
                parent_id = r["id"]
        logging.info(
            "Created API {} with id {} and resource parent id {}".format(
                api_name, api_id, parent_id
            )
        )

        # Run AWS-specific part of building code.
        urls = [
            self.create_lambda_function(
                benchmark,
                fname,
                api_id,
                parent_id,
                code_location,
                code_size,
                memory,
                timeout,
                experiment_config,
            )
            for fname in function_names
        ]
        return urls, api_id

    def update_function_config(self, fname: str, timeout: int, memory: int):
        self.get_lambda_client()
        self.client.update_function_configuration(
            FunctionName=fname, Timeout=timeout, MemorySize=memory
        )
