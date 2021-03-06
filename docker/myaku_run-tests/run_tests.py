"""Runs all of the test suites for the Myaku project."""

import argparse
import logging
import os
import random
import string
import subprocess
import sys
import time
from subprocess import CompletedProcess
from typing import Any, Dict, List, NamedTuple

import colorlog
import docker
import pytest
from docker.models.containers import Container

_log = logging.getLogger(__name__)

_TEST_RUNNER_CONTAINER_NAME = 'test-runner'

# _LOGGING_FORMAT = '%(asctime)s:%(levelname)s: %(message)s'
_LOGGING_FORMAT = '%(log_color)s%(message)s'

_MYAKU_PROJECT_DIR_ENV_VAR = 'MYAKU_PROJECT_DIR'
_REVERSEPROXY_HOST_ENV_VAR = 'REVERSEPROXY_HOST'

# Relative to the Myaku project dir root
_REACT_APP_REL_DIR = 'myakuweb-clientapp'

# Relative to the Myaku project dir root
_MYAKUWEB_SELENIUM_TESTS_REL_DIR = 'selenium_tests'

_TEST_STACK_NAME_PREFIX = 'myaku_test_'

# Docker objects that can be removed by getting them from a list() with the
# docker client and then calling remove().
# Containers is not on this list because list() must be called with the all
# option to get all containers.
_NORMAL_REMOVE_DOCKER_OBJECTS = [
    'volumes',
    'networks',
    'configs',
    'secrets',
    'services'
]

# Shell color codes
_RED = '\033[31m'
_GREEN = '\033[32m'
_BLUE = '\033[34m'
_ENDC = '\033[0m'

# Script arg parser setup
arg_parser = argparse.ArgumentParser(
    description='Runs all of the test suites for the Myaku project.'
)
arg_parser.add_argument(
    '-e', '--use-existing-images',
    action='store', choices=['prod', 'test'],
    help=(
        'Instead of building new prod images for the tests, use existing '
        'images. '
        'If "prod" is specified, uses the images specified in the  base '
        'docker compose file (./docker/docker-compose.yml). '
        'If "test" is specified, uses the images with the :test tag.'
    )
)
arg_parser.add_argument(
    '-r', '--remove-only',
    action='store_true',
    help=(
        'Removes all test docker objects including volumes and exits without '
        'running any tests.'
    )
)


def red_text(text: str) -> str:
    """Make text green in console output."""
    return _RED + text + _ENDC


def green_text(text: str) -> str:
    """Make text green in console output."""
    return _GREEN + text + _ENDC


def blue_text(text: str) -> str:
    """Make text blue in console output."""
    return _BLUE + text + _ENDC


def log_blue(msg: str, *args, **kwargs) -> None:
    """Log an info message in blue in the console output."""
    _log.info(blue_text(msg), *args, **kwargs)


def setup_logger() -> logging.Logger:
    """Set up the logger for the script.

    Writes the log to stdout.

    Returns:
        The setup logger to use for the script.
    """
    logger = logging.getLogger('run_tests')
    logger.setLevel(logging.DEBUG)

    stream_handler = colorlog.StreamHandler()
    stream_handler.setLevel(logging.DEBUG)
    stream_handler.setFormatter(colorlog.ColoredFormatter(
        _LOGGING_FORMAT,
        reset=True,
        log_colors={
            'DEBUG': '',
            'INFO': '',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'red,bg_white',
        }
    ))
    logger.addHandler(stream_handler)

    return logger


def install_node_app_deps(node_app_dir: str) -> None:
    """Install all dependencies for a nodejs app using npm.

    Args:
        node_app_dir: Directory containing the node app.

    Raises:
        RuntimeError: npm install returned a non-zero exit code when attempting
            to install the dependencies for the app.
    """
    preinstall_dir = os.getcwd()
    try:
        os.chdir(node_app_dir)
        installCompleted = subprocess.run(['npm', 'install'], text=True)
        if (installCompleted.returncode != 0):
            raise RuntimeError(
                f'npm install for node app "{os.getcwd()}" failed'
            )
    finally:
        os.chdir(preinstall_dir)


def run_docker_subprocess(cmd: List[str], **kwargs) -> CompletedProcess:
    """Run a subprocess for a docker CLI command.

    Suppresses output of the command and checks the return code.

    Args:
        cmd: Docker CLI command to run.
        kwargs: Additional kwargs to pass to subprocess.run.

    Returns:
        The completed process object for the subprocess.

    Raises:
        RuntimeError: The docker process exited with a non-zero return code.
    """
    completed = subprocess.run(
        cmd, capture_output=True, text=True, **kwargs
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f'Docker process exited with non-zero return code: {completed}'
        )

    return completed


def init_docker_swarm() -> None:
    """Initialize docker swarm on the docker engine for the tests."""
    docker_client = docker.from_env()

    # If swarm.attrs is not empty, the docker engine is already part of a
    # swarm.
    if docker_client.swarm.attrs:
        return

    docker_client.swarm.init()


def get_test_stacks() -> List[str]:
    """Get a list of all of the currently live test stacks.

    A stack is considered a test stack if its name begins with
    _TEST_STACK_NAME_PREFIX.
    """
    completed = run_docker_subprocess(['docker', 'stack', 'ls'])

    # First line of stdout is the column headers, so skip first line
    test_stacks = []
    for line in completed.stdout.splitlines()[1:]:
        stack_name = line.split()[0]
        if stack_name.startswith(_TEST_STACK_NAME_PREFIX):
            test_stacks.append(stack_name)

    return test_stacks


def attempt_remove_docker_obj(docker_obj: Any) -> None:
    """Attempt to remove the given docker object.

    Can remove any docker object as long as it has a remove() method.

    If the attempt to remove was unsuccessful, logs the reason as a warning and
    takes no other action.
    """
    try:
        docker_obj.remove()
    except docker.errors.APIError as e:
        _log.warning(
            'Unable to remove docker object %s due to error: %s', docker_obj, e
        )


def teardown_test_stacks() -> None:
    """Attempt to removes any docker objects created by test stacks.

    Attempts to remove all stacks, services, containers, secrets, configs,
    networks, and volumes created by test stacks.

    If a test stack object is unable to be removed, logs a warning and
    continues.
    """
    test_stacks = get_test_stacks()
    for test_stack in test_stacks:
        _log.debug('Removing %s test stack...', test_stack)
        run_docker_subprocess(['docker', 'stack', 'rm', test_stack])

    docker_client = docker.from_env()
    for container in docker_client.containers.list(all=True):
        if container.name.startswith(_TEST_STACK_NAME_PREFIX):
            attempt_remove_docker_obj(container)

    for obj_type in _NORMAL_REMOVE_DOCKER_OBJECTS:
        for obj in getattr(docker_client, obj_type).list():
            if obj.name.startswith(_TEST_STACK_NAME_PREFIX):
                attempt_remove_docker_obj(obj)


class DockerImageBuildSpec(NamedTuple):
    """Specification for how to build a docker image.

    Attributes:
        image_name: Name to use for the image. Should not include the tag.
        dockerfile_path: Path to the dockerfile to build the image.
    """
    image_name: str
    dockerfile_path: str


class TestMyakuStack(object):
    """A Myaku docker stack for testing use."""

    # Number of characters to use in the randomly-generated portion of the test
    # stack name.
    _STACK_NAME_RAND_SECTION_LEN = 8

    # Seconds to wait for all of the containers for the stack to be running
    # before raising an error.
    _CONTAINER_STARTUP_TIMEOUT = 30

    # Paths are relative to the Myaku project root directory.
    _STACK_IMAGE_BUILD_SPECS = [
        DockerImageBuildSpec(
            image_name='friedrice2/myaku_crawler',
            dockerfile_path='docker/myaku_crawler/Dockerfile.crawler'
        ),
        DockerImageBuildSpec(
            image_name='friedrice2/myaku_rescore',
            dockerfile_path='docker/myaku_rescore/Dockerfile.rescore'
        ),
        DockerImageBuildSpec(
            image_name='friedrice2/myaku_web',
            dockerfile_path='docker/myaku_web/Dockerfile.myakuweb'
        ),
        DockerImageBuildSpec(
            image_name='friedrice2/myaku_web-worker',
            dockerfile_path='docker/myaku_web/Dockerfile.myakuweb'
        ),
        DockerImageBuildSpec(
            image_name='friedrice2/myaku_redis.first-page-cache',
            dockerfile_path=(
                'docker/myaku_redis.first-page-cache/'
                'Dockerfile.redis.first-page-cache'
            )
        ),
        DockerImageBuildSpec(
            image_name='friedrice2/myaku_redis.next-page-cache',
            dockerfile_path=(
                'docker/myaku_redis.next-page-cache/'
                'Dockerfile.redis.next-page-cache'
            )
        ),
        DockerImageBuildSpec(
            image_name='friedrice2/myaku_nginx.reverseproxy',
            dockerfile_path=(
                'docker/myaku_nginx.reverseproxy/Dockerfile.nginx.reverseproxy'
            )
        ),
        DockerImageBuildSpec(
            image_name='friedrice2/myaku_mongo.crawldb',
            dockerfile_path=(
                'docker/myaku_mongo.crawldb/Dockerfile.mongo.crawldb'
            )
        ),
        DockerImageBuildSpec(
            image_name='friedrice2/mongobackup',
            dockerfile_path='docker/mongobackup/Dockerfile.mongobackup'
        ),
    ]

    # Test images only need to be built once per script invocation, so track
    # whether they've been built or not as a class variable.
    _test_images_built = False

    # If None, will build new tests images to use for the test stack.
    # If "prod", will use the images specified in the base docker compose file
    #   (./docker/docker-compose.yml).
    # If "test", will use the images with the :test tag.
    use_existing_images = None

    @staticmethod
    def apply_script_args(script_args: argparse.Namespace) -> None:
        """Apply the script args related to test stack creation.

        Args:
            config_args: A argparse.Namespace with the args given to this
                script.
        """
        TestMyakuStack.use_existing_images = script_args.use_existing_images

    def __init__(self) -> None:
        """Deploy a new Myaku docker stack for testing."""
        self._docker_client = docker.from_env()
        self._myaku_project_dir = os.environ[_MYAKU_PROJECT_DIR_ENV_VAR]

        rand_section = ''.join(random.choices(
            string.hexdigits.lower(),
            k=self._STACK_NAME_RAND_SECTION_LEN
        ))
        self.stack_name = f'myaku_test_{rand_section}'

        if TestMyakuStack.use_existing_images is None:
            self._build_stack_images()

        try:
            if TestMyakuStack.use_existing_images == 'prod':
                self._deploy_stack_using_prod_images()
            else:
                self._deploy_stack_using_test_images()
        except BaseException:
            # Make sure we remove any part of stack that got deployed if there
            # was any error during the deployment process.
            self.teardown()
            raise

    def __enter__(self) -> 'TestMyakuStack':
        """Build and deploy a Myaku test stack."""
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback) -> None:
        """Tear down the deployed test stack."""
        self.teardown()

    def teardown(self) -> None:
        """Tear down the deployed test stack.

        Does NOT remove the volumes for the test stack.
        """
        _log.debug('Removing %s test stack...', self.stack_name)
        run_docker_subprocess(['docker', 'stack', 'rm', self.stack_name])

    def _build_stack_images(self) -> None:
        """Build the test prod images to use in the stack."""
        if TestMyakuStack._test_images_built:
            return

        for image_spec in self._STACK_IMAGE_BUILD_SPECS:
            tagged_image_name = image_spec.image_name + ':test'
            abs_dockerfile_path = os.path.join(
                self._myaku_project_dir, image_spec.dockerfile_path
            )

            build_cmd = [
                'docker', 'build',
                '-f', abs_dockerfile_path,
                '-t', tagged_image_name,
                '--target', 'prod',
                self._myaku_project_dir
            ]

            _log.debug('Building %s image...', tagged_image_name)
            run_docker_subprocess(build_cmd)

        TestMyakuStack._test_images_built = True

    def _get_no_image_test_compose(self) -> str:
        """Get the test docker compose file data without the image: lines.

        The file data can then be used with the base docker compose file to run
        the Myaku stack with the current prod images.
        """
        test_compose_filepath = os.path.join(
            self._myaku_project_dir, 'docker/docker-compose.test.yml'
        )
        with open(test_compose_filepath, 'r') as test_compose_file:
            test_compose_lines = test_compose_file.readlines()

        no_image_test_compose_lines = []
        for line in test_compose_lines:
            if 'image: ' not in line:
                no_image_test_compose_lines.append(line)

        return ''.join(no_image_test_compose_lines)

    def _deploy_stack_using_prod_images(self) -> None:
        """Deploy the Myaku test stack using current prod images.

        The images specified in the current base docker compose file
        (./docker/docker-compose.yml) are used.
        """
        _log.debug('Using prod images specified in docker/docker-compose.yml')
        no_image_test_compose = self._get_no_image_test_compose()
        base_compose_filepath = os.path.join(
            self._myaku_project_dir, 'docker/docker-compose.yml'
        )

        deploy_cmd = [
            'docker', 'stack', 'deploy',
            '-c', base_compose_filepath, '-c', '-',
            self.stack_name
        ]

        _log.debug('Deploying Myaku test stack %s...', self.stack_name)
        run_docker_subprocess(deploy_cmd, input=no_image_test_compose)
        self._wait_for_all_containers_running()
        log_blue('Stack %s created', self.stack_name)

    def _deploy_stack_using_test_images(self) -> None:
        """Deploy the Myaku test stack using the :test tagged images."""
        _log.debug('Using :test tagged images')
        test_compose_filepath = os.path.join(
            self._myaku_project_dir, 'docker/docker-compose.test.yml'
        )
        base_compose_filepath = os.path.join(
            self._myaku_project_dir, 'docker/docker-compose.yml'
        )

        deploy_cmd = [
            'docker', 'stack', 'deploy',
            '-c', base_compose_filepath,
            '-c', test_compose_filepath,
            self.stack_name
        ]

        _log.debug('Deploying Myaku test stack %s...', self.stack_name)
        run_docker_subprocess(deploy_cmd)
        self._wait_for_all_containers_running()
        log_blue('Stack %s created', self.stack_name)

    def _log_stack_debug_info(self) -> None:
        """Log debug info for all of the services in the stack."""
        services = self._docker_client.services.list(
            filters={'name': self.stack_name}
        )
        for service in services:
            _log.debug('\n%s service ps:', service.name)
            subprocess.run(
                ['docker', 'service', 'ps', '--no-trunc', service.name],
                text=True
            )

            log_gen = service.logs(stdout=True, stderr=True, timestamps=True)
            logs = b''.join(list(log_gen)).decode()
            _log.debug('\n%s service logs:\n%s', service.name, logs)

    def _wait_for_all_containers_running(self) -> None:
        """Wait for all containers for the test stack to be running.

        Deploying a docker stack creates all the services for the stack, but it
        can still take some time after the services are created for all of
        their containers to be in a running state.

        This function will wait until all services for the stack have their
        containers in a running state.

        Will timeout and raise an error if _CONTAINER_STARTUP_TIMEOUT seconds
        have passed and not all of the containers are in the running state.
        """
        _log.debug(
            'Waiting for stack %s containers to start...', self.stack_name
        )
        waited_secs = 0
        while waited_secs < self._CONTAINER_STARTUP_TIMEOUT:
            services = self._docker_client.services.list(
                filters={'name': self.stack_name}
            )
            all_running = True
            for service in services:
                for task in service.tasks():
                    if ('Status' not in task
                            or 'State' not in task['Status']
                            or task['Status']['State'] != 'running'):
                        all_running = False
                        break
                if not all_running:
                    break

            if all_running:
                break
            time.sleep(1)
            waited_secs += 1

        if not all_running:
            self._log_stack_debug_info()
            raise RuntimeError(
                f'Test stack {self.stack_name} containers not all running '
                f'after timeout of {self._CONTAINER_STARTUP_TIMEOUT} seconds. '
                f'See logged stack debug info for errors.'
            )
        _log.debug('Stack %s containers all started', self.stack_name)

    def _get_container(self, name_prefix: str) -> Container:
        """Get the running container with the given name prefix."""
        for container in self._docker_client.containers.list():
            if container.name.startswith(name_prefix):
                return container

        raise RuntimeError(f'Could not get {name_prefix} container')

    def get_crawler_container(self) -> Container:
        """Get the crawler service container for this test stack."""
        return self._get_container(self.stack_name + '_crawler')

    def link_to_reverseproxy(self) -> str:
        """Create a network linking to the reverseproxy service.

        Creates a bridge network with docker between the test runner container
        running this script and the reverseproxy service container in the test
        stack.

        Returns:
            The hostname that can be used to reach the reverseproxy service
            from the test runner container currently running this script.
        """
        reverseproxy_container = self._get_container(
            self.stack_name + '_reverseproxy'
        )
        test_runner_container = self._get_container(
            _TEST_RUNNER_CONTAINER_NAME
        )

        net_name = self.stack_name + '_test_runner_net'
        _log.debug(
            'Creating %s network linking test runner to test stack...',
            net_name
        )
        net = self._docker_client.networks.create(net_name, driver='bridge')
        net.connect(reverseproxy_container, aliases=['reverseproxy'])
        net.connect(test_runner_container, aliases=['test-runner'])

        return 'reverseproxy'


class TestRunner(object):
    """Run the Myaku test suites and track their results."""

    def __init__(self, use_existing_images: bool) -> None:
        """Set up the test runner to track test results.

        Args:
            use_existing_images: Instead of building new prod images for the
                tests, use the existing images specified in the current base
                docker compose file (./docker/docker-compose.yml).
        """
        self._use_existing_images = use_existing_images
        self._test_results: Dict[str, str] = {}

        self._myaku_project_dir = os.environ[_MYAKU_PROJECT_DIR_ENV_VAR]

    def run_react_app_tests(self) -> None:
        """Run all of the unit tests for the MyakuWeb React app."""
        app_dir = os.path.join(self._myaku_project_dir, _REACT_APP_REL_DIR)

        log_blue('\nInstalling dependencies for MyakuWeb React app...')
        install_node_app_deps(app_dir)

        log_blue('\nRunning jest unit tests for MyakuWeb React app...')
        pretest_dir = os.getcwd()
        try:
            os.chdir(app_dir)
            testCompleted = subprocess.run(['npm', 'test'], text=True)
            self._log_test_result('react app unit', testCompleted.returncode)
        finally:
            os.chdir(pretest_dir)

    def run_crawler_tests(self) -> None:
        """Run all of the tests for the Myaku crawler service."""
        with TestMyakuStack() as test_stack:
            crawler_container = test_stack.get_crawler_container()
            self._run_crawler_unit_tests(crawler_container)
            self._run_crawler_end_to_end_test(crawler_container)

    def run_web_tests(self) -> None:
        """Run all of the end-to-end tests for MyakuWeb.

        Uses a selenium web driver to run the tests.
        """
        with TestMyakuStack() as test_stack:
            reverseproxy_host = test_stack.link_to_reverseproxy()
            os.environ[_REVERSEPROXY_HOST_ENV_VAR] = reverseproxy_host

            self._run_web_selenium_tests()

    def _run_crawler_unit_tests(self, container: Container) -> None:
        """Run the unit test suite for the crawler.

        Args:
            container: The crawler container to run the tests in.
        """
        log_blue(
            '\nRunning unit pytests for crawler in %s container...',
            container.name.split('.')[0]
        )
        exec_cmd = [
            'docker', 'exec', '-it', container.short_id,
            '/bin/bash', '-c', '$PYTHON_BIN -m pytest myaku/tests/unit'
        ]
        completed = subprocess.run(exec_cmd, text=True)

        self._log_test_result('crawler unit', completed.returncode)

    def _run_crawler_end_to_end_test(self, container: Container) -> None:
        """Run the end to end test for the crawler.

        Args:
            container: The crawler container to run the tests in.
        """
        log_blue(
            '\nRunning end-to-end pytest for crawler in %s container...',
            container.name.split('.')[0]
        )
        exec_cmd = [
            'docker', 'exec', '-it', container.short_id,
            '/bin/bash', '-c',
            '$PYTHON_BIN -m pytest --no-print-logs myaku/tests/end_to_end'
        ]
        completed = subprocess.run(exec_cmd, text=True)

        self._log_test_result('crawler end-to-end', completed.returncode)

    def _run_web_selenium_tests(self) -> None:
        """Run the selenium web driver tests for Myaku web.

        Unlike the crawler tests, these tests are run inside the test runner
        container, but the tests use selenium to make http requests to the test
        stack reverseproxy to test the myaku web service in order to closely
        simulate real requests.
        """
        log_blue('\nRunning selenium pytests for MyakuWeb...')
        test_dir = os.path.join(
            self._myaku_project_dir,
            _MYAKUWEB_SELENIUM_TESTS_REL_DIR
        )
        returncode = pytest.main([test_dir])

        self._log_test_result('web selenium', returncode)

    def _log_test_result(self, test_name: str, returncode: int) -> None:
        """Log the results for a test.

        Args:
            test_name: Name of the test.
            returncode: Return code for the test. 0 means all tests passed, and
            non-0 means all tests did not pass.
        """
        if returncode == 0:
            _log.info(
                'Test result for %s: %s\n', test_name, green_text('PASSED')
            )
            self._test_results[test_name] = 'PASSED'
        else:
            _log.info(
                'Test result for %s: %s\n', test_name, red_text('FAILED')
            )
            self._test_results[test_name] = 'FAILED'

    def log_overall_results(self) -> int:
        """Log the overall test results.

        Returns:
            0 if all tests passed, 1 if not all tests passed.
        """
        failed_tests = []
        for test_name, result in self._test_results.items():
            if result == 'FAILED':
                failed_tests.append(test_name)

        if len(failed_tests) == 0:
            _log.info(
                '\nOverall test results: ' + green_text('ALL TESTS PASSED')
            )
            return 0
        else:
            _log.info('\nOverall test results: ' + red_text('TEST FAILURE'))
            _log.info('Failed test suites: %s', ', '.join(failed_tests))
            return 1


def main() -> None:
    """Run all of the test suites for the Myaku project."""
    script_args = arg_parser.parse_args()
    TestMyakuStack.apply_script_args(script_args)

    _log.debug('Initializing docker swarm...')
    init_docker_swarm()

    _log.debug('Cleaning up any previous test docker objects...')
    teardown_test_stacks()
    if script_args.remove_only:
        sys.exit(0)

    test_runner = TestRunner(script_args.use_existing_images)
    test_runner.run_react_app_tests()
    test_runner.run_crawler_tests()
    test_runner.run_web_tests()

    returncode = test_runner.log_overall_results()
    sys.exit(returncode)


if __name__ == '__main__':
    _log = setup_logger()
    try:
        main()
    except Exception:
        _log.exception('Unhandled exception in main')
        raise
