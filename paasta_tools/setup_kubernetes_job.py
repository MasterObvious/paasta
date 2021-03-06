#!/usr/bin/env python
# Copyright 2015-2018 Yelp Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Usage: ./setup_kubernetes_job.py <service.instance> [options]

Command line options:

- -d <SOA_DIR>, --soa-dir <SOA_DIR>: Specify a SOA config dir to read from
- -v, --verbose: Verbose output
"""
import argparse
import logging
import sys
from typing import Optional
from typing import Sequence
from typing import Tuple

from paasta_tools.kubernetes_tools import create_deployment
from paasta_tools.kubernetes_tools import ensure_paasta_namespace
from paasta_tools.kubernetes_tools import KubeClient
from paasta_tools.kubernetes_tools import KubeDeployment
from paasta_tools.kubernetes_tools import list_all_deployments
from paasta_tools.kubernetes_tools import load_kubernetes_service_config_no_cache
from paasta_tools.kubernetes_tools import update_deployment
from paasta_tools.utils import decompose_job_id
from paasta_tools.utils import DEFAULT_SOA_DIR
from paasta_tools.utils import InvalidJobNameError
from paasta_tools.utils import load_system_paasta_config
from paasta_tools.utils import NoConfigurationForServiceError
from paasta_tools.utils import NoDeploymentsAvailable
from paasta_tools.utils import NoDockerImageError
from paasta_tools.utils import SPACER

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Creates marathon jobs.')
    parser.add_argument(
        'service_instance_list', nargs='+',
        help="The list of marathon service instances to create or update",
        metavar="SERVICE%sINSTANCE" % SPACER,
    )
    parser.add_argument(
        '-d', '--soa-dir', dest="soa_dir", metavar="SOA_DIR",
        default=DEFAULT_SOA_DIR,
        help="define a different soa config directory",
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        dest="verbose", default=False,
    )
    args = parser.parse_args()
    return args


def main() -> None:
    args = parse_args()
    soa_dir = args.soa_dir
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    # system_paasta_config = load_system_paasta_config()
    kube_client = KubeClient()

    ensure_paasta_namespace(kube_client)
    setup_kube_succeeded = setup_kube_deployments(
        kube_client=kube_client,
        service_instances=args.service_instance_list,
        soa_dir=soa_dir,
    )
    sys.exit(0 if setup_kube_succeeded else 1)


def setup_kube_deployments(
    kube_client: KubeClient,
    service_instances: Sequence[str],
    soa_dir: str=DEFAULT_SOA_DIR,
) -> bool:
    suceeded = True
    if service_instances:
        deployments = list_all_deployments(kube_client)
    for service_instance in service_instances:
        try:
            service, instance, _, __ = decompose_job_id(service_instance)
        except InvalidJobNameError:
            log.error("Invalid service instance specified. Format is service%sinstance." % SPACER)
            suceeded = False
        else:
            if reconcile_kubernetes_deployment(
                kube_client=kube_client,
                service=service,
                instance=instance,
                kube_deployments=deployments,
                soa_dir=soa_dir,
            )[0]:
                suceeded = False
    return suceeded


def reconcile_kubernetes_deployment(
    kube_client: KubeClient,
    service: str,
    instance: str,
    kube_deployments: Sequence[KubeDeployment],
    soa_dir: str,
) -> Tuple[int, Optional[int]]:
    try:
        service_instance_config = load_kubernetes_service_config_no_cache(
            service,
            instance,
            load_system_paasta_config().get_cluster(),
            soa_dir=soa_dir,
        )
    except NoDeploymentsAvailable:
        log.debug("No deployments found for %s.%s in cluster %s. Skipping." %
                  (service, instance, load_system_paasta_config().get_cluster()))
        return 0, None
    except NoConfigurationForServiceError:
        error_msg = "Could not read kubernetes configuration file for %s.%s in cluster %s" % \
                    (service, instance, load_system_paasta_config().get_cluster())
        log.error(error_msg)
        return 1, None

    try:
        formatted_deployment = service_instance_config.format_kubernetes_app()
    except NoDockerImageError:
        error_msg = (
            "Docker image for {0}.{1} not in deployments.json. Exiting. Has Jenkins deployed it?\n"
        ).format(
            service,
            instance,
        )
        log.error(error_msg)
        return (1, None)

    desired_deployment = KubeDeployment(
        service=service,
        instance=instance,
        git_sha=formatted_deployment.metadata.labels["git_sha"],
        config_sha=formatted_deployment.metadata.labels["config_sha"],
        replicas=formatted_deployment.spec.replicas,
    )

    if not (service, instance) in [(kd.service, kd.instance) for kd in kube_deployments]:
        log.debug(f"{desired_deployment} does not exist so creating")
        create_deployment(
            kube_client=kube_client,
            formatted_deployment=formatted_deployment,
        )
        return 0, None
    elif desired_deployment not in kube_deployments:
        log.debug(f"{desired_deployment} exists but config_sha or git_sha doesn't match or number of instances changed")
        update_deployment(
            kube_client=kube_client,
            formatted_deployment=formatted_deployment,
        )
        return 0, None
    else:
        log.debug(f"{desired_deployment} is up to date, no action taken")
        return 0, None


if __name__ == "__main__":
    main()
