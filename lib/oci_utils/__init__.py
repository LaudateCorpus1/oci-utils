# oci-utils
#
# Copyright (c) 2017, 2019 Oracle and/or its affiliates. All rights reserved.
# Licensed under the Universal Permissive License v 1.0 as shown
# at http://oss.oracle.com/licenses/upl.

import logging
import os
import os.path

import enum

from .impl import read_config as read_configuration
from .impl import setup_logging as _setup_logging

# file with a list IQNs to ignore
__ignore_file = "/var/lib/oci-utils/ignore_iqns"
# file with chap user names and passwords
__chap_password_file = "/var/lib/oci-utils/chap_secrets"

_METADATA_ENDPOINT = '169.254.169.254'
_MAX_VOLUMES_LIMIT = 32
_configuration = read_configuration()

oci_regions = {
    'ams': 'ams - eu-amsterdam-1 (Amsterdam, The Netherlands)',
    'arn': 'arn - eu-stockholm-1 (Stockholm, Norway)',
    'auh': 'auh - me-abu-dhabi-1 (Abu Dhabi, United Arab Emirates)',
    'bom': 'bom - ap-mumbai-1 (Mumbai, India)',
    'cwl': 'cwl - uk-cardiff-1 (Newport, UK)',
    'dxb': 'dxb - me-dubai-1 (Duabi, UAE)',
    'fra': 'fra - eu-frankfurt-1 (Frankfurt, Germany)',
    'gru': 'gru - sa-saopaulo-1 (Sao Paulo, Brazil)',
    'hyd': 'hyd - ap-hyderabad-1 (Hyderabad, India)',
    'iad': 'iad - us-ashburn-1 (Ashburn, VA, USA)',
    'icn': 'icn - ap-seoul-1 (Seoul, South Korea)',
    'jed': 'jed - me-jeddah-1 (Jeddah, Saudi Arabia)',
    'jnb': 'jnb - af-johannesburg-1 (Johannesburg, South Africa)',
    'kix': 'kix - ap-osaka-1 (Osaka, Japan)',
    'lhr': 'lhr - uk-london-1 (London, UK)',
    'lin': 'lin - eu-milan-1 (Milan, Italy)',
    'mct': 'mct - me-dcc-muscat-1 (Muscat, Oman)',
    'mel': 'mel - ap-melbourne-1 (Melbourne, Australia)',
    'mrs': 'mrs - eu-marseille-1 (Marseille, France)',
    'mtz': 'mtz - il-jerusalem-1 (Jerusalem, Israel)',
    'nri2': 'nri2 - ap-osaka-2 (Osaka, Japan)',
    'nri': 'nri - ap-ibaraki-1 (Osaka, Japan)',
    'nrt': 'nrt - ap-tokyo-1 (Tokyo, Japan)',
    'phx': 'phx - us-phoenix-1 (Phoenix, AZ, USA)',
    'scl': 'scl - sa-santiago-1 (Santiago, Chile)',
    'sin': 'sin - ap-singapore-1 (Singapore, Singapore)',
    'sjc': 'sjc - us-sanjose-1 (San Jose, CA, USA)',
    'syd': 'syd - ap-sydney-1 (Sydney, Australia)',
    'vcp': 'vcp - sa-vinhedo-1 (Vinhedo, Brazil)',
    'wga': 'wga - ap-canberra-1 (Canberra, Australia)',
    'yny': 'yny - ap-chuncheon-1 (Chuncheon, South Korea)',
    'yul': 'yul - ca-montreal-1 (Montreal, Canada)',
    'yyz': 'yyz - ca-toronto-1 (Toronto, Canada)',
    'zrh': 'zrh - eu-zurich-1 (Zurich, Switzerland)'}


class OCI_ATTACHMENT_STATE(enum.Enum):
    """ Attachment state defintions.
    """
    ATTACHING = 0
    ATTACHED = 1
    DETACHING = 2
    DETACHED = 3
    NOT_ATTACHED = 4


class OCI_RESOURCE_STATE(enum.Enum):
    """ Resource state definitions.
    """
    PROVISIONING = 0
    AVAILABLE = 1
    RESTORING = 2
    TERMINATING = 3
    TERMINATED = 4
    FAULTY = 5


class OCI_INSTANCE_STATE(enum.Enum):
    """ Resource state definitions.
    """
    PROVISIONING = 0
    RUNNING = 1
    STARTING = 2
    STOPPING = 3
    STOPPED = 4
    CREATING_IMAGE = 5
    TERMINATING = 6
    TERMINATED = 7


class OCI_COMPARTEMENT_STATE(enum.Enum):
    """ Compartment state definitions.
    """
    CREATING = 0
    ACTIVE = 1
    INACTIVE = 2
    DELETING = 3
    DELETED = 4


class OCI_VOLUME_SIZE_FMT(enum.Enum):
    """ Volume size format definitions.
    """
    HUMAN = 0
    GB = 1
    MB = 2


def find_exec_in_path(exec_name):
    """
    Find an executable in the path.

    Parameters
    ----------
    exec_name: str
        The name of the executable.

    Returns
    -------
        str: the full path of the executable.
    """
    path = os.getenv('PATH').split(':')
    result = None
    for rootdir in path:
        for root, folder, files in os.walk(rootdir):
            if exec_name in files:
                result = os.path.join(root, exec_name)
            break
        if result:
            break
    return result


def _set_proxy():
    """
    Set the proxy for OCI metadata service access.

    Metadata service(and instance principal auth) won't work through a proxy.
    1. Add the metadata endpoint to NO_PROXY env var
    2. Set the http_proxy and https_proxy environment vars according to
       oci-utils configuration

    Returns
    -------
        No return value.
    """
    #
    if 'NO_PROXY' in os.environ:
        os.environ['NO_PROXY'] += ',%s' % _METADATA_ENDPOINT
    else:
        os.environ['NO_PROXY'] = _METADATA_ENDPOINT

    # check if there are proxy settings in the config files
    try:
        proxy = _configuration.get('network', 'http_proxy')
        os.environ['http_proxy'] = proxy
    except Exception:
        pass

    try:
        proxy = _configuration.get('network', 'https_proxy')
        os.environ['https_proxy'] = proxy
    except Exception:
        pass


def _setup_env():
    """
    Protect ourself for sub process executions
    """
    os.environ['LC_ALL'] = 'C'


_setup_logging(('_OCI_UTILS_DEBUG' in os.environ) or (_configuration.has_section('ocid') and
                                                      _configuration.has_option('ocid', 'debug') and
                                                      _configuration.getboolean('ocid', 'debug')
                                                      ))


_set_proxy()
_setup_env()
