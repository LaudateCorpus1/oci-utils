#!/usr/bin/env python2.7

# oci-utils
#
# Copyright (c) 2018 Oracle and/or its affiliates. All rights reserved.
# Licensed under the Universal Permissive License v 1.0 as shown at http://oss.oracle.com/licenses/upl.

"""
High level wrapper around the OCI Python SDK
"""

import sys
import os
import re
import logging
from logging.handlers import SysLogHandler
from time import sleep
import six
import oci_utils
import oci_utils.metadata
import json
from .auth_helper import OCIAuthProxy
from .exceptions import OCISDKError

HAVE_OCI_SDK = True
try:
    import oci as oci_sdk
except ImportError:
    HAVE_OCI_SDK = False

# authentication methods
DIRECT = 'direct'
PROXY = 'proxy'
IP = 'ip'
AUTO = 'auto'
NONE = 'None'

MAX_VOLUMES_LIMIT = 32


class OCIAPIObject(object):
    """
    Ancestor class for most OCI objects
    """
    _ignore_dict_items = ["swagger_types", "attribute_map"] 

    _complex_items = [ "launch_options", "source_details" ]
    #_complex_items = [ oci_sdk.core.models.instance_source_via_image_details.InstanceSourceViaImageDetails, oci_sdk.core.models.launch_options.LaunchOptions ]

    def __init__(self):
        pass

    def __dict__(self):
        return OCIAPIObject.get_dict_recursive(self)
    
    @staticmethod
    def get_dict_recursive(obj):
        '''
        get dict for object which has embedded complex object. 
        '''
        try:
            data_dict = {}
            if hasattr(obj, 'data'):
                x =  obj.data
            else:
                x = obj
            for key in vars(x):
                value = getattr(x, key)
                if key.startswith('_'):
                    key = key[1:]
                # Handle complex types
                if key in OCIAPIObject._ignore_dict_items:
                   continue 
                if type(value) in [int, bool]:
                    data_dict[key] = value
                elif isinstance(value, six.string_types):
                    data_dict[key] = value.strip()
                elif value is None:
                    data_dict[key] = ''
                elif  key in OCIAPIObject._complex_items:
                    data_dict[key] = OCIAPIObject.get_dict_recursive(value)
                else:
                    data_dict[key] = value
            return data_dict
        except:
            return None

    def get_display_name(self):
        try:
            return self.data.display_name
        except:
            return None

    def get_compartment(self):
        try:
            return self.oci_session.get_compartment(self.data.compartment_id)
        except:
            return None

class OCISession(object):
    """
    High level OCI Cloud API operations
    """
    def __init__(self, config_file='~/.oci/config', config_profile='DEFAULT',
                 auth_method=None, debug=False):
        global HAVE_OCI_SDK
        if not HAVE_OCI_SDK:
            raise OCISDKError('Package python-oci-sdk not installed')
        self.debug = debug
        self._setup_logging(debug=debug, syslog=False)
        self.config_file = config_file
        self.config_profile = config_profile
        self.compartments = None
        self.instances = None
        # max time spent waiting for the sdk thread lock
        self.sdk_lock_timeout = int(60)
        self.vcns = None
        self.subnets = None
        self.volumes = None
        self.identity_client = None
        self.compute_client = None
        self.network_client = None
        self.block_storage_client = None
        self.object_storage_client = None
        self.oci_utils_config = oci_utils.read_config()
        try:
            self.metadata = oci_utils.metadata.metadata(get_public_ip=False).get(silent=True).get()
        except Exception as e:
            self.metadata = None
        self.oci_config = {}
        self.signer = None
        self.auth_method = auth_method
        self.auth_status = ""
        try:
            # see if auth_method was set in oci-utils.conf
            self.auth_method = self.oci_utils_config.get('auth',
                                                         'auth_method')
        except:
            if self.auth_method is None:
                self.auth_method = AUTO
        if not self.metadata:
            # code is running outside OCI, must have direct auth:
            self.auth_method = self._get_auth_method(auth_method=DIRECT)
        else:
            self.auth_method = self._get_auth_method(self.auth_method)
        if self.auth_method == NONE:
            if self.auth_status:
                raise OCISDKError(self.auth_status)
            raise OCISDKError('Failed to authenticate with the Oracle Cloud Infrastructure service')

        self.tenancy_ocid = None
        if 'tenancy' in self.oci_config :
            # DIRECT or PROXY auth
            self.tenancy_ocid = self.oci_config['tenancy']
        elif self.signer is not None:
            # IP auth
            self.tenancy_ocid = self.signer.tenancy_id
        elif 'instance' in self.metadata:
            # fall back to the instance's own compartment_id
            # We will only see the current compartment, but better than nothing
            self.tenancy_ocid = self.metadata['instance']['compartmentId']

    def this_shape(self):
        try:
            return self.metadata['instance']['shape']
        except:
            return None

    def _setup_logging(self, debug=False, syslog=False):
        self.logger = logging.getLogger('oci_utils.oci_api')

        if syslog:
            handler = SysLogHandler(address='/dev/log',
                                facility=SysLogHandler.LOG_DAEMON)
        else:
            handler = logging.StreamHandler(stream=sys.stderr)

        if debug:
            self.logger.setLevel(logging.DEBUG)
            handler.setLevel(logging.DEBUG)
        else:
            self.logger.setLevel(logging.INFO)
            handler.setLevel(logging.INFO)
            
        formatter = logging.Formatter('%(name)s - %(levelname)s '
                                          '- %(message)s')
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)

    @staticmethod
    def _read_oci_config(fname, profile='DEFAULT'):
        """
        Read the OCI config file.
        """
        full_fname = os.path.expanduser(fname)
        if os.path.exists(full_fname):
            try:
                oci_config = oci_sdk.config.from_file(full_fname, profile)
                return oci_config
            except oci_sdk.exceptions.ConfigFileNotFound as e:
                raise OCISDKError("Unable to read OCI config file: %s" % e)
        else:
            raise OCISDKError("Config file %s not found" % full_fname)

    def set_sdk_call_timeout(self,timeout):
        if int(timeout) < 0:
            raise OCISDKError("Invalid value for timeout in sdk_call()")
        self.sdk_lock_timeout = int(timeout)

    def sdk_call(self, client_func, *args, **kwargs):
        """
        Make an SDK call in a thread-safe way.
        Give up acquiring the thread lokc after timeout seconds.
        OCISDKError is raised if the timeout is reached.
        timeout=0 means wait forever.
        """
        timeout = self.sdk_lock_timeout
        oci_utils.lock_thread(timeout=timeout)
        # for loggin purposes
        call_name = str(client_func)
        try:
            call_name=client_func.__name__
        except:
            pass
        try:
            result = client_func(*args, **kwargs)
            next = result
            while hasattr(next, 'data') and next.has_next_page:
                next = client_func(*args, page=next.next_page,**kwargs)
                result.data.extend(next.data);
        except Exception as e:
            oci_utils.release_thread()
            self.logger.debug("API call %s failed: %s" % (call_name, e))
            raise
        oci_utils.release_thread()
        return result
            
    def _get_auth_method(self, auth_method=None):
        """
        Determine how (or if) we can authenticate.
        If auth_method is provided, and is not AUTO then test if the given
        auth_method works.
        return one of oci_api.DIRECT, oci_api.PROXY, oci_api.IP or oci_api.NONE
        (IP is instance principals)
        """
        if auth_method is not None:
            # testing a specific auth method
            if auth_method == DIRECT:
                if self._direct_authenticate():
                    return DIRECT
                else:
                    return NONE
            elif auth_method == PROXY:
                if self._proxy_authenticate():
                    return PROXY
                else:
                    return NONE
            elif auth_method == IP:
                if self._ip_authenticate():
                    return IP
                else:
                    return NONE

        # Try the direct method first
        try:
            if self._direct_authenticate():
                return DIRECT
        except:
            # ignore any errors and try a different method
            pass

        # If we are root, we can try proxy call through the oci_sdk_user user
        if os.geteuid() == 0:
            try:
                if self._proxy_authenticate():
                    return PROXY
            except:
                # ignore any errors and try a different method
                pass

        # finally, try instance principals
        try:
            if self._ip_authenticate():
                return IP
        except:
            pass

        # no options left
        return NONE

    def _proxy_authenticate(self):
        """
        Use the auth helper to get config settings and keys
        Return True for success, False for failure
        """
        sdk_user = self.oci_utils_config.get('auth', 'oci_sdk_user')
        try:
            proxy = OCIAuthProxy(sdk_user)
            self.oci_config = proxy.get_config()
        except Exception as e:
            self.auth_status += "Authentication as user %s failed: %s\n" \
                                % (sdk_user, e)
            self.logger.debug('Proxy auth failed: %s' % e)
            return False
        try:
            self.identity_client = self.sdk_call(
                oci_sdk.identity.IdentityClient,
                self.oci_config)
        except Exception as e:
            self.auth_status += "Cannot create identity client as user %s\n" \
                                % sdk_user
            self.logger.debug('ID client with proxy auth failed: %s' % e)
            return False
        self.auth_status += "Authentication as user %s succeeded\n" % sdk_user
        return True
        
    def _direct_authenticate(self):
        """
        Authenticate with the OCI SDK.
        Return True for success, False for failure
        Raises an exception when it appears to work but API calls fail
        (indicates misconfiguration)
        """
        # raises OCISDKError
        try:
            self.oci_config = self._read_oci_config(fname=self.config_file,
                                                    profile=self.config_profile)
        except Exception as e:
            self.auth_status += "Config file authentication failed: %s\n" % e
            self.logger.debug('Cannot read oci config file: %s' % e)
            return False

        try:
            self.identity_client = self.sdk_call(
                oci_sdk.identity.IdentityClient,
                self.oci_config)
            self.auth_status += "Config file (direct) authentication succeeded\n"
            return True
        except Exception as e:
            self.auth_status += "Cannot create identity client: %s\n" % e
            self.logger.debug('Direct auth failed: %s' % e)
            return False

        # test that it works
        if not self.metadata:
            #not in an instance, return without test.
            return true

        try:
            inst_id = self.metadata['instance']['id']
            cc = self.get_compute_client()
            inst = self.sdk_call(cc.get_instance, instance_id=inst_id)
        except oci_sdk.exceptions.ServiceError as e:
            self.auth_status += "Cannot make OCI API calls: %s\n" % e
            raise OCISDKError('Cannot make OCI API calls: %s' % e.message)

        self.auth_status += "Config file (direct) authentication succeeded\n"
        return True

    def _ip_authenticate(self):
        """
        Authenticate with the OCI SDK.
        raise OCISDKError if failed.
        """
        self.signer = \
            oci_sdk.auth.signers.InstancePrincipalsSecurityTokenSigner()
        self.identity_client = self.sdk_call(
            oci_sdk.identity.IdentityClient,
            config={}, signer=self.signer)
        try:
            inst = self.this_instance()
        except Exception as e:
            self.auth_status += "Instance Principals authentication: %s\n" % e
            self.logger.debug('IP auth failed: %s' % e)
            # reset compute client set by this_instance()
            self.compute_client = None
            return False
        if inst is not None:
            self.auth_status += "Instance Principals (IP) authentication succeeded\n"
            return True
        
        return False


    def all_compartments(self, refresh=False):
        """
        Return a list of OCICompartment objects.
        """
        if self.compartments is not None and not refresh:
            return self.compartments

        self.compartments = []
        try: 
            compartments_data = oci_sdk.pagination.list_call_get_all_results(
                self.identity_client.list_compartments,
                compartment_id=self.tenancy_ocid).data
        except Exception as e:
            self.logger.error('%s' % e)
            return self.compartments


        #compartments_data = self.identity_client.list_compartments(
        #    compartment_id=self.tenancy_ocid).data
        for c_data in compartments_data:
            self.compartments.append(OCICompartment(session=self,
                                                    compartment_data=c_data))
        return self.compartments

    def find_compartments(self, display_name, refresh=False):
        """
        Return a list of OCICompartment-s with a matching display_name regexp
        """
        dn_re = re.compile(display_name)
        compartments = []
        for comp in self.all_compartments(refresh=refresh):
            res = dn_re.search(comp.data.display_name)
            if res is not None:
                compartments.append(comp)
        return compartments

    def find_vcns(self, display_name, refresh=False):
        """
        Return a list of OCIVCN-s with a matching display_name regexp
        """
        dn_re = re.compile(display_name)
        vcns = []
        for vcn in self.all_vcns(refresh=refresh):
            res = dn_re.search(vcn.data.display_name)
            if res is not None:
                vcns.append(vcn)
        return vcns

    def all_subnets(self, refresh=False):
        """
        Return a list of OCISubnet objects.
        """
        if self.subnets is not None and not refresh:
            return self.subnets

        subnets = []
        for compartment in self.all_compartments(refresh=refresh):
            comp_subnets = compartment.all_subnets()
            if comp_subnets is not None:
                subnets += comp_subnets
        self.subnets = subnets
        return subnets

    def get_compute_client(self):
        if self.compute_client is None:
            if self.signer is not None:
                self.compute_client = \
                    oci_sdk.core.compute_client.ComputeClient(
                        config=self.oci_config, signer=self.signer)
            else:
                self.compute_client = \
                    oci_sdk.core.compute_client.ComputeClient(
                        config=self.oci_config)
        return self.compute_client
    
    def get_network_client(self):
        if self.network_client is None:
            if self.signer is not None:
                self.network_client = \
                    oci_sdk.core.virtual_network_client.VirtualNetworkClient(
                        config=self.oci_config, signer=self.signer)
            else:
                self.network_client = \
                    oci_sdk.core.virtual_network_client.VirtualNetworkClient(
                        config=self.oci_config)
        return self.network_client
    
    def get_block_storage_client(self):
        if self.block_storage_client is None:
            if self.signer is not None:
                self.block_storage_client = \
                    oci_sdk.core.blockstorage_client.BlockstorageClient(
                        config=self.oci_config, signer=self.signer)
            else:
                self.block_storage_client = \
                    oci_sdk.core.blockstorage_client.BlockstorageClient(
                        config=self.oci_config)
        return self.block_storage_client
    
    def get_object_storage_client(self):
        if self.object_storage_client is None:
            if self.signer is not None:
                self.object_storage_client = \
                    oci_sdk.object_storage.object_storage_client.\
                    ObjectStorageClient(
                        config=self.oci_config, signer=self.signer)
            else:
                self.object_storage_client = \
                    oci_sdk.object_storage.object_storage_client.\
                    ObjectStorageClient(
                        config=self.oci_config)
        return self.object_storage_client
    
    def all_instances(self, refresh=False):
        if self.instances is not None and not refresh:
            return self.instances

        instances = []
        for compartment in self.all_compartments(refresh=refresh):
            comp_instances = compartment.all_instances()
            if comp_instances is not None:
                instances += comp_instances
        self.instances = instances
        return instances


    def update_instance_metadata(self, instance_id=None, **kwargs):
        '''
        Parameters:
            instance_id: optional if you run on the instance.
            kwargs: dict of updating fields.
        Return:
            metadata updated.
        '''

        if instance_id is None:
            try:
                instance_id = self.metadata['instance']['id']
            except Exception as e:
                self.logger.error('No instance id. Please run in an instance or provide instance-id.\n')
                return None
        details = {}
        for key in OCIInstance.settable_field_type:
            if key in kwargs.keys():
                details[key] = kwargs[key]

        cc = self.get_compute_client()

        try:
            result = self.sdk_call(
                cc.update_instance,
                instance_id=instance_id,
                update_instance_details=details,
            ).data
        except Exception as e:
            self.logger.error('Failed to set metadata: %s. ' % e.message)
            return None
            
        return OCIInstance(self, result).get_metadata()

    def find_instances(self, display_name, refresh=False):
        """
        Return a list of OCIInstance-s with a matching display_name regexp
        """
        dn_re = re.compile(display_name)
        instances = []
        for instance in self.all_instances(refresh=refresh):
            res = dn_re.search(instance.data.display_name)
            if res is not None:
                instances.append(instance)
        return instances

    def find_volumes(self, display_name=None,
                     iqn=None, refresh=False):
        """
        Return a list of OCIVolume-s with a matching display_name regexp
        and/or IQN
        """
        if display_name is None and iqn is None:
            return []
        dn_re = None
        if display_name is not None:
            dn_re = re.compile(display_name)
        volumes = []
        for volume in self.all_volumes(refresh=refresh):
            if dn_re is not None:
                # check if display_name matches
                res = dn_re.search(volume.data.display_name)
                if res is None:
                    # no match
                    continue
            if iqn is not None:
                if volume.get_iqn() != iqn:
                    # iqn doesn't match
                    continue
            # all filter conditions match
            volumes.append(volume)
        return volumes

    def find_subnets(self, display_name, refresh=False):
        """
        Return a list of OCISubnet-s with matching the display_name regexp
        """
        dn_re = re.compile(display_name)
        subnets = []
        for subnet in self.all_subnets(refresh=refresh):
            res = dn_re.search(subnet.data.display_name)
            if res is not None:
                subnets.append(subnet)
        return subnets

    def all_vcns(self, refresh=False):
        if self.vcns is not None and not refresh:
            return self.vcns

        vcns = []
        for compartment in self.all_compartments(refresh=refresh):
            comp_vcns = compartment.all_vcns()
            if comp_vcns is not None:
                vcns += comp_vcns
        self.vcns = vcns
        return vcns

    def all_volumes(self, refresh=False):
        if self.volumes is not None and not refresh:
            return self.volumes

        volumes = []
        for compartment in self.all_compartments(refresh=refresh):
            comp_volumes = compartment.all_volumes()
            if comp_volumes is not None:
                volumes += comp_volumes
        self.volumes = volumes
        return volumes

    def this_instance(self, refresh=False):
        if self.metadata is None:
            return None
        try:
            my_instance_id = self.metadata['instance']['id']
        except Exception as e:
            self.logger.debug('Cannot find my instance ID: %s' % e)
            return None

        return self.get_instance(instance_id=my_instance_id,
                                 refresh=refresh)

    def this_compartment(self, refresh=False):
        if self.metadata is None:
            return None
        try:
            my_compartment_id = self.metadata['instance']['compartmentId']
        except:
            return None

        try:
            comp_data = self.identity_client.get_compartment(
                compartment_id=my_compartment_id).data
        except Exception as e:
            self.logger.error(str(e))
            return None

        return OCICompartment(session=self,
                              compartment_data=comp_data)

    def this_availability_domain(self):
        if self.metadata is None:
            return None
        return self.metadata['instance']['availabilityDomain']

    def get_tenancy_ocid(self):
        """
        return the ocid of the tenancy
        """
        return self.tenancy_ocid

    def this_region(self):
        if self.metadata is None:
            return None
        try:
            return self.metadata['instance']['region']
        except:
            return None

    def get_instance(self, instance_id, refresh=False):
        cc = self.get_compute_client()
        if not refresh and self.instances:
            # return from cache
            for i in self.instances:
                if i.get_ocid() == instance_id:
                    return i
        try:
            instance_data = self.sdk_call(cc.get_instance,
                                          instance_id=instance_id).data
            return OCIInstance(self, instance_data)
        except Exception as e:
            self.logger.error('Failed to fetch instance: %s. \nCheck your connection and settings.' % e)

        return None

    def get_subnet(self, subnet_id, refresh=False):
        # FIXME: use virtual_network_client.get_subnet directly
        for sn in self.all_subnets(refresh=refresh):
            if sn.get_ocid() == subnet_id:
                return sn
        return None

    def get_volume(self, volume_id, refresh=False):
        """
        Return an OCIVolume object representing the volume with the given
        ocid, or None if the volume is not found.
        """

        if self.volumes is not None and not refresh:
            for vol in self.volumes:
                if vol.volume_ocid == volume_id:
                    return vol

        bsc = self.get_block_storage_client()
        cc = self.get_compute_client()

        try:
            vol_data = self.sdk_call(bsc.get_volume,
                                     volume_id=volume_id).data
        except oci_sdk.exceptions.ServiceError:
            return None

        if vol_data.lifecycle_state == "TERMINATED":
            return None

        try:
            v_att_list = self.sdk_call(cc.list_volume_attachments,
                compartment_id=vol_data.compartment_id,
                volume_id=vol_data.id).data
        except:
            # can't find any attachments for this volume
            return OCIVolume(self, volume_data=vol_data)

        # find the latest attachment entry for this volume
        v_att_data = None
        for v_att in v_att_list:
            if v_att_data is None:
                v_att_data = v_att
                continue
            if v_att.time_created > v_att_data.time_created:
                v_att_data = v_att

        return OCIVolume(self,
                         volume_data=vol_data,
                         attachment_data=v_att_data)

    def get_compartment(self, compartment_id, refresh=False):
        if not refresh and self.compartments:
            # return from cache
            for i in self.compartments:
                if i.get_ocid() == compartment_id:
                    return i
        for c in self.all_compartments(refresh=refresh):
            if c.get_ocid() == compartment_id:
                return c
        return None

    def get_vcn(self, vcn_id, refresh=False):
        if not refresh and self.vcns:
            # return from cache
            for i in self.vcns:
                if i.get_ocid() == vcn_id:
                    return i
        for c in self.all_vcns(refresh=refresh):
            if c.get_ocid() == vcn_id:
                return c
        return None

    def get_vnic(self, vnic_id, refresh=False):
        # FIXME: use list_vnic_attachments and get_vnic directly
        for c in self.all_compartments(refresh=refresh):
            for v in c.all_vnics(refresh=refresh):
                if v.get_ocid() == vnic_id:
                    return v
        return None

    def create_volume(self, compartment_id, availability_domain,
                      size, display_name=None, wait=True):
        '''
        create a new OCI Storage Volume in the given compartment and
        availability_domain, of the given size (GBs, >=50), and with
        the given display_name.
        Return an OCIVolume object.
        '''
        bsc = self.get_block_storage_client()
        cvds = oci_sdk.core.models.CreateVolumeDetails(
            availability_domain=availability_domain,
            compartment_id=compartment_id,
            size_in_gbs=size,
            display_name=display_name)
        try:
            vol_data = self.sdk_call(bsc.create_volume,
                                     create_volume_details=cvds)
            if wait:
                while vol_data.data.lifecycle_state != 'AVAILABLE':
                    sleep(2)
                    vol_data = self.sdk_call(bsc.get_volume,
                                             volume_id=vol_data.data.id)
            return OCIVolume(self, vol_data.data)
        except oci_sdk.exceptions.ServiceError as e:
            raise OCISDKError('Failed to create volume: %s' % e.message)

class OCICompartment(OCIAPIObject):
    def __init__(self, session, compartment_data):
        """
        compartment_data:

        id (str)                -- The value to assign to the
                                   id property of this Compartment.
        compartment_id (str)    -- The value to assign to the compartment_id
                                   property of this Compartment.
        name (str)              -- The value to assign to the name property of
                                   this Compartment.
        description (str)       -- The value to assign to the description
                                   property of this Compartment.
        time_created (datetime) -- The value to assign to the time_created
                                   property of this Compartment.
        lifecycle_state (str)   -- The value to assign to the lifecycle_state
                                   property of this Compartment.
                                   Allowed values for this property are:
                                   "CREATING", "ACTIVE", "INACTIVE",
                                   "DELETING", "DELETED",
                                   'UNKNOWN_ENUM_VALUE'.
                                   Any unrecognized values returned by a
                                   service will be mapped to
                                   'UNKNOWN_ENUM_VALUE'.
        inactive_status (int)   -- The value to assign to the inactive_status
                                   property of this Compartment.
        freeform_tags (dict(str, str)) -- The value to assign to the
                                   freeform_tags property of this Compartment.
        defined_tags (dict(str, dict(str, object))) -- The value to assign
                                   to the defined_tags property of this
                                   Compartment.

        """
        self.oci_session = session
        self.tenancy_id = compartment_data.compartment_id
        self.compartment_ocid = compartment_data.id
        self.data = compartment_data
        self.subnets = None
        self.instances = None
        self.vcns = None
        self.vnics = None
        self.volumes = None

    def __str__(self):
        return "Compartment '%s' (%s)" % (self.data.name,
                                          self.compartment_ocid)

    def get_ocid(self):
        return self.compartment_ocid

    def all_instances(self, refresh=False):
        if self.instances is not None and not refresh:
            return self.instances
        if self.data.lifecycle_state != 'ACTIVE':
            return None

        cc = self.oci_session.get_compute_client()

        # Note: the user may not have permission to list instances
        # in this compartment, so ignoring ServiceError exceptions
        instances = []
        try:
            instances_data = self.oci_session.sdk_call(
                cc.list_instances,
                compartment_id=self.compartment_ocid)
            for i_data in instances_data.data:
                if i_data.lifecycle_state == 'TERMINATED':
                    continue
                instances.append(OCIInstance(self.oci_session, i_data))
        except oci_sdk.exceptions.ServiceError:
            # ignore these, it means the current user has no
            # permission to list the instances in the compartment
            pass
        self.instances = instances
        return instances

    def all_subnets(self, refresh=False):
        if self.subnets is not None and not refresh:
            return self.subnets
        if self.data.lifecycle_state != 'ACTIVE':
            return None

        subnets = []
        for vcn in self.all_vcns(refresh=refresh):
            vcn_subnets = vcn.all_subnets()
            if vcn_subnets is not None:
                subnets += vcn_subnets
        self.subnets = subnets
        return subnets

    def all_vnics(self, refresh=False):
        if self.vnics is not None and not refresh:
            return self.vnics
        if self.data.lifecycle_state != 'ACTIVE':
            return None

        vnics = []
        for instance in self.all_instances(refresh=refresh):
            inst_vnics = instance.all_vnics(refresh=refresh)
            if inst_vnics:
                vnics += inst_vnics
        self.vnics = vnics
        return vnics

    def all_vcns(self, refresh=False):
        if self.vcns is not None and not refresh:
            return self.vcns
        if self.data.lifecycle_state != 'ACTIVE':
            return None

        nc = self.oci_session.get_network_client()

        # Note: the user may not have permission to list vcns
        # in this compartment, so ignoring ServiceError exceptions
        vcns = []
        try:
            vcns_data = self.oci_session.sdk_call(nc.list_vcns,
                compartment_id=self.compartment_ocid)
            for v_data in vcns_data.data:
                if v_data.lifecycle_state != 'AVAILABLE':
                    continue
                vcns.append(OCIVCN(self.oci_session, v_data))
        except oci_sdk.exceptions.ServiceError:
            # ignore these, it means the current user has no
            # permission to list the vcns in the compartment
            pass
        self.vcns = vcns
        return vcns

    def all_volumes(self, refresh=False, availability_domain=None):
        if self.volumes is not None and availability_domain is None \
           and not refresh:
            return self.volumes
        if self.data.lifecycle_state != 'ACTIVE':
            return None

        bsc = self.oci_session.get_block_storage_client()
        cc = self.oci_session.get_compute_client()

        # Note: the user may not have permission to list volumes
        # in this compartment, so ignoring ServiceError exceptions
        bs = []
        try:
            if availability_domain :
                bs_data = self.oci_session.sdk_call(bsc.list_volumes,
                                availability_domain=availability_domain,
                                compartment_id=self.compartment_ocid)
            else:
                bs_data = self.oci_session.sdk_call(bsc.list_volumes,
                                compartment_id=self.compartment_ocid)
            for v_data in bs_data.data:
                if v_data.lifecycle_state != 'AVAILABLE':
                    continue
                v_att_list = self.oci_session.sdk_call(
                    cc.list_volume_attachments,
                    compartment_id=self.compartment_ocid,
                    volume_id=v_data.id).data
                v_att_data = None
                for v_att in v_att_list:
                    if v_att_data is None:
                        v_att_data = v_att
                        continue
                    if v_att.time_created > v_att_data.time_created:
                        v_att_data = v_att
                bs.append(OCIVolume(self.oci_session,
                                    volume_data=v_data,
                                    attachment_data=v_att_data))
        except oci_sdk.exceptions.ServiceError:
            # ignore these, it means the current user has no
            # permission to list the volumes in the compartment
            pass
        if availability_domain is None:
            self.volumes = bs
        return bs

    def create_volume(self, availability_domain, size, display_name=None,
                      wait=True):
        '''
        create a new OCI Storage Volume in this compartment
        '''
        return self.oci_session.create_volume(
            compartment_id=self.get_ocid(),
            availability_domain=availability_domain,
            size=size,
            display_name=display_name,
            wait=wait)

class OCIInstance(OCIAPIObject):
    #Notes: dict can be json formatted string or file.
    settable_field_type={
        'displayName': str,
        'metadata': dict,
        'extendedMetadata': dict
    }

    lower_settable_fields = {key.lower():key for key in settable_field_type }
    def __init__(self, session, instance_data):
        """
        instance_data:

        availability_domain (str) -- The value to assign to the
                                     availability_domain property of
                                     this Instance.
        compartment_id (str)      -- The value to assign to the compartment_id
                                     property of this Instance.
        defined_tags (dict(str, dict(str, object))) -- The value to assign
                                     to the defined_tags property of this
                                     Instance.
        display_name (str)        -- The value to assign to the display_name
                                     property of this Instance.
        extended_metadata (dict(str, object)) -- The value to assign to the
                                     extended_metadata property of this
                                     Instance.
        freeform_tags (dict(str, str)) -- The value to assign to the
                                     freeform_tags property of this Instance.
        id (str)                  -- The value to assign to the id property
                                     of this Instance.
        image_id (str)            -- The value to assign to the image_id
                                     property of this Instance.
        ipxe_script (str)         -- The value to assign to the ipxe_script
                                     property of this Instance.
        launch_mode (str)         -- The value to assign to the launch_mode
                                     property of this Instance. Allowed values
                                     for this property are: "NATIVE",
                                     "EMULATED", "CUSTOM",
                                     'UNKNOWN_ENUM_VALUE'. Any unrecognized
                                     values returned by a service will be
                                     mapped to 'UNKNOWN_ENUM_VALUE'.
        launch_options (LaunchOptions) -- The value to assign to the
                                     launch_options property of this Instance.
        lifecycle_state (str)     -- The value to assign to the lifecycle_state
                                     property of this Instance. Allowed values
                                     for this property are: "PROVISIONING",
                                     "RUNNING", "STARTING", "STOPPING",
                                     "STOPPED", "CREATING_IMAGE",
                                     "TERMINATING", "TERMINATED",
                                     'UNKNOWN_ENUM_VALUE'. Any unrecognized
                                     values returned by a service will be
                                     mapped to 'UNKNOWN_ENUM_VALUE'.
        metadata (dict(str, str)) -- The value to assign to the metadata
                                     property of this Instance.
        region (str)              -- The value to assign to the region
                                     property of this Instance.
        shape (str)               -- The value to assign to the shape
                                     property of this Instance.
        source_details (InstanceSourceDetails) -- The value to assign to the
                                     source_details property of this Instance.
        time_created (datetime)   -- The value to assign to the time_created
                                     property of this Instance.
        """
        self.oci_session = session
        self.data = instance_data
        self.vnics = None
        self.subnets = None
        self.volumes = None
        self.metadata = None
        self.secondary_private_ips = None
        self.instance_ocid = instance_data.id

    def __str__(self):
        return "Instance '%s' (%s)" % (self.data.display_name,
                                          self.instance_ocid)

    def get_ocid(self):
        return self.instance_ocid

    def get_hostname(self):
        return self.data.display_name

    def get_state(self):
        return self.data.lifecycle_state

    def get_public_ip(self):
        '''
        return the public IP address of the primary VNIC
        '''
        for v in self.all_vnics():
            if v.is_primary():
                return v.get_public_ip()
        return None

    def all_public_ips(self):
        '''
        return all public IP addresses associated with this instance
        '''
        ips = []
        for v in self.all_vnics():
            ip = v.get_public_ip()
            if ip is not None:
                ips.append(ip)
        return ips

    def all_vnics(self, refresh=False):
        if self.vnics is not None and not refresh:
            return self.vnics

        vnics = []
        cc = self.oci_session.get_compute_client()
        nc = self.oci_session.get_network_client()
        try:
            vnic_atts = self.oci_session.sdk_call(cc.list_vnic_attachments,
                compartment_id=self.data.compartment_id,
                instance_id=self.instance_ocid)
        except Exception as e:
            return []
        for v_a_data in vnic_atts.data:
            if v_a_data.lifecycle_state != 'ATTACHED':
                continue
            try:
                vnic_data = self.oci_session.sdk_call(nc.get_vnic,
                    v_a_data.vnic_id).data
                vnics.append(OCIVNIC(self.oci_session, vnic_data=vnic_data,
                                     attachment_data=v_a_data))
            except oci_sdk.exceptions.ServiceError:
                # ignore these, it means the current user has no
                # permission to list the instances in the compartment
                pass
        self.vnics = vnics
        return vnics

    def find_private_ip(self, ip_address, refresh=False):
        '''
        Find a secondary private IP based on its IP address
        '''
        for priv_ip in self.all_private_ips():
            if priv_ip.get_address() == ip_address:
                return priv_ip
        return None

    def all_private_ips(self, refresh=False):
        '''
        return a list of secondary private IPs assigned to this instance
        '''
        if self.secondary_private_ips is not None and not refresh:
            return self.secondary_private_ips

        private_ips = []
        for vnic in self.all_vnics(refresh=refresh):
            pips = vnic.all_private_ips(refresh=refresh)
            private_ips += pips

        self.secondary_private_ips = private_ips
        return private_ips

    def all_subnets(self, refresh=False):
        if self.subnets is not None and not refresh:
            return self.subnets

        subnet_ids = []
        subnets = []
        for vnic in self.all_vnics(refresh=refresh):
            if vnic.data.subnet_id not in subnet_ids:
                subnet_ids.append(vnic.data.subnet_id)
                subnets.append(
                    self.oci_session.get_subnet(vnic.data.subnet_id,
                                                refresh=refresh))
        self.subnets = subnets
        return subnets

    def all_volumes(self, refresh=False):
        if self.volumes is not None and not refresh:
            return self.volumes

        bsc = self.oci_session.get_block_storage_client()
        cc = self.oci_session.get_compute_client()

        # Note: the user may not have permission to list volumes
        # so ignoring ServiceError exceptions
        try:
            v_att_list = self.oci_session.sdk_call(
                cc.list_volume_attachments,
                compartment_id=self.data.compartment_id,
                instance_id=self.instance_ocid).data
        except oci_sdk.exceptions.ServiceError:
            # the user has no permission to list volumes
            self.volumes = []
            return self.volumes

        # multiple volume attachments may exist for the same
        # volume and instance.  For each one, we need to find
        # the most recent one
        v_att_data = {}
        for v_att in v_att_list:
            if v_att.volume_id not in v_att_data:
                v_att_data[v_att.volume_id] = v_att
                continue
            if v_att_data[v_att.volume_id].time_created < \
               v_att.time_created:
                v_att_data[v_att.volume_id] = v_att

        vols = []
        for vol_id in v_att_data.keys():
            # only include volumes that are properly attached, not
            # attaching or detaching or anything like that
            if v_att_data[vol_id].lifecycle_state != "ATTACHED":
                continue

            try:
                vol_data = self.oci_session.sdk_call(
                    bsc.get_volume,
                    volume_id=vol_id).data
            except oci_sdk.exceptions.ServiceError:
                continue
            vols.append(OCIVolume(self.oci_session,
                                  volume_data=vol_data,
                                  attachment_data=v_att_data[vol_id]))

        self.volumes = vols
        return vols

    def attach_volume(self, volume_id, use_chap=False,
                      display_name=None, wait=True):
        """
        attach the given volume to this instance
        """
        if self.max_volumes_reached():
            raise OCISDKError('This instance reached its max_volumes.')

        av_det = oci_sdk.core.models.AttachIScsiVolumeDetails(
            type="iscsi",
            use_chap=use_chap,
            volume_id=volume_id,
            instance_id=self.get_ocid(),
            display_name=display_name
            )
        cc = self.oci_session.get_compute_client()
        try:
            vol_att = self.oci_session.sdk_call(cc.attach_volume, av_det)
            if wait:
                while vol_att.data.lifecycle_state != "ATTACHED":
                    sleep(2)
                    vol_att = self.oci_session.sdk_call(
                        cc.get_volume_attachment,
                        vol_att.data.id)
            return self.oci_session.get_volume(vol_att.data.volume_id,
                                               refresh=True)
        except oci_sdk.exceptions.ServiceError as e:
            raise OCISDKError('Failed to attach volume: %s' % e.message)


    def attach_vnic(self, private_ip=None, subnet_id=None, nic_index=0,
                    display_name=None, assign_public_ip=False,
                    hostname_label=None, skip_source_dest_check=False,
                    wait=True):
        """
        Create and attach a VNIC to this device.
        Use sensible defaults:
          - subnet_id: if None, use the same subnet as the primary VNIC
          - private_ip: if None, the next available IP in the subnet

        Returns an OCIVNIC object on success.
        Raises OCISDKError on error
        """
        if display_name is None and hostname_label is not None:
            display_name = hostname_label
        if hostname_label is None and display_name is not None:
            hostname = os.popen("/usr/bin/hostname").read().strip()
            hostname_label = hostname + "-" + display_name
            # list of acceptable chars in a host name
            hostname_chars = 'abcdefghijklmnopqrstuvwxyz' \
                             'ABCDEFGHIJKLMNOPQRSTUVWXYZ' \
                             '0123456789-'
            hostname_label = \
                ''.join([c for c in hostname_label if c in hostname_chars])
        # step 1: choose a subnet
        if subnet_id is None:
            instance_subnets = self.all_subnets()
            if private_ip is not None:
                # choose the subnet that the ip belongs to
                for sn in instance_subnets:
                    if sn.ip_matches(private_ip):
                        subnet_id = sn.get_ocid()
                if subnet_id is None:
                    # no suitable subnet found for the IP address
                    raise OCISDKError('No suitable subnet found for IP address '
                                      '%s' % private_ip)
            else:
                # choose one of the subnets the instance currently uses
                if len(instance_subnets) == 1:
                    subnet_id = instance_subnets[0].get_ocid()
                else:
                    # FIXME: for now just choose the first one,
                    # but we can probably be cleverer
                    subnet_id = instance_subnets[0].get_ocid()
        cc = self.oci_session.get_compute_client()
        create_vnic_details = oci_sdk.core.models.CreateVnicDetails(
            assign_public_ip=assign_public_ip,
            display_name=display_name,
            hostname_label=hostname_label,
            private_ip=private_ip,
            skip_source_dest_check=skip_source_dest_check,
            subnet_id=subnet_id)
        attach_vnic_details = oci_sdk.core.models.AttachVnicDetails(
            create_vnic_details=create_vnic_details,
            display_name=display_name,
            nic_index=nic_index,
            instance_id=self.get_ocid())
        try:
            resp = self.oci_session.sdk_call(cc.attach_vnic,
                                             attach_vnic_details)
            v_att = self.oci_session.sdk_call(cc.get_vnic_attachment,
                                              resp.data.id)
            if wait:
                while v_att.data.lifecycle_state != "ATTACHED":
                    sleep(2)
                    v_att = self.oci_session.sdk_call(cc.get_vnic_attachment,
                                                      resp.data.id)
            return self.oci_session.get_vnic(v_att.data.vnic_id, refresh=True)
        except oci_sdk.exceptions.ServiceError as e:
            raise OCISDKError('Failed to attach new VNIC: %s' % e.message)

    def max_volumes_reached(self):
        max_volumes = int(self.oci_session.oci_utils_config.get('iscsi','max_volumes'))
        if max_volumes > MAX_VOLUMES_LIMIT:
            max_volumes = MAX_VOLUMES_LIMIT
        if len(self.all_volumes()) >= max_volumes:
            return True
        return False

    def create_volume(self, size, display_name=None):
        '''
        create a new OCI Storage Volume and attach it to this instance
        '''
        if self.max_volumes_reached():
            raise OCISDKError('This instance reached its max_volumes.')

        vol = self.oci_session.create_volume(
            compartment_id=self.data.compartment_id,
            availability_domain=self.data.availability_domain,
            size=size,
            display_name=display_name,
            wait=True)

        try:
            vol = vol.attach_to(instance_id=self.get_ocid())
        except Exception as e:
            vol.destroy()
            return None
        return vol

    def get_metadata(self, get_public_ip=False, refresh=False):
        '''
        get metadata for this instance, 
        return OCIMetadata
        '''
        if self.metadata != None and not refresh:
            return self.metadata

        meta = {}
        meta['instance'] = self.__dict__()
        
        #get vnics
        vnics = self.all_vnics(refresh=refresh)
        vnics_l = []
        for vnic in vnics:
            vnic_i = vnic.__dict__()
            vnic_a = json.loads(vnic.att_data.__str__())
            vnic_i['nic_index'] = vnic_a['nic_index']
            vnic_i['vlan_tag'] = vnic_a['vlan_tag']
            vnics_l.append(vnic_i)
        meta['vnics'] =  vnics_l

        #get public ips
        if get_public_ip:
            meta['public_ip'] = self.get_public_ip()
       
        self.metadata = oci_utils.metadata.OCIMetadata(meta, convert=True)
        return self.metadata

class OCIVCN(OCIAPIObject):
    def __init__(self, session, vcn_data):
        """
        vcn_data:

        cidr_block (str)            -- The value to assign to the cidr_block
                                       property of this Vcn.
        compartment_id (str)        -- The value to assign to the
                                       compartment_id property of this Vcn.
        default_dhcp_options_id (str) -- The value to assign to the
                                       default_dhcp_options_id property of
                                       this Vcn.
        default_route_table_id (str) -- The value to assign to the
                                       default_route_table_id property of
                                       this Vcn.
        default_security_list_id (str) -- The value to assign to the
                                       default_security_list_id property of
                                       this Vcn.
        defined_tags (dict(str, dict(str, object))) -- The value to assign
                                       to the defined_tags property of this Vcn.
        display_name (str)          -- The value to assign to the display_name
                                       property of this Vcn.
        dns_label (str)             -- The value to assign to the dns_label
                                       property of this Vcn.
        freeform_tags (dict(str, str)) -- The value to assign to the
                                       freeform_tags property of this Vcn.
        id (str)                    -- The value to assign to the id property
                                       of this Vcn.
        lifecycle_state (str)       -- The value to assign to the
                                       lifecycle_state property of this Vcn.
                                       Allowed values for this property are:
                                       "PROVISIONING", "AVAILABLE",
                                       "TERMINATING", "TERMINATED",
                                       'UNKNOWN_ENUM_VALUE'. Any unrecognized
                                       values returned by a service will be
                                       mapped to 'UNKNOWN_ENUM_VALUE'.
        time_created (datetime)    -- The value to assign to the time_created
                                      property of this Vcn.
        vcn_domain_name (str)      -- The value to assign to the
                                      vcn_domain_name property of this Vcn.

        """
        self.compartment_name = None
        self.oci_session = session
        self.data = vcn_data
        self.vcn_ocid = vcn_data.id
        self.subnets = None
        self.security_lists = None

    def __str__(self):
        return "VCN '%s' (%s)" % (self.data.display_name,
                                  self.vcn_ocid)

    def set_compartment_name(self, name):
         self.compartment_name = name

    def get_ocid(self):
        return self.vcn_ocid

    def all_subnets(self, refresh=False):
        if self.subnets is not None and not refresh:
            return self.subnets
        
        nc = self.oci_session.get_network_client()

        # Note: the user may not have permission to list instances
        # in this compartment, so ignoring ServiceError exceptions
        subnets = []
        try:
            subnets_data = self.oci_session.sdk_call(
                nc.list_subnets,
                compartment_id=self.data.compartment_id,
                vcn_id=self.vcn_ocid)
            for s_data in subnets_data.data:
                subnets.append(OCISubnet(self.oci_session, s_data))
        except oci_sdk.exceptions.ServiceError:
            # ignore these, it means the current user has no
            # permission to list the instances in the compartment
            pass
        self.subnets = subnets
        return subnets

    def all_security_lists(self, refresh=False):
        if self.security_lists is not None and not refresh:
            return self.security_lists

        nc = self.oci_session.get_network_client()

        # Note: the user may not have permission to list instances
        # in this compartment, so ignoring ServiceError exceptions
        security_lists = dict()
        try:
            security_list_data = self.oci_session.sdk_call(
                nc.list_security_lists,
                compartment_id=self.data.compartment_id,
                vcn_id=self.vcn_ocid)
            for s_data in security_list_data.data:
                security_lists.setdefault(s_data.id, OCISecurityList(self.oci_session, s_data))
        except oci_sdk.exceptions.ServiceError:
            # ignore these, it means the current user has no
            # permission to list the instances in the compartment
            pass
        self.security_lists = security_lists
        return security_lists


class OCIVNIC(OCIAPIObject):
    def __init__(self, session, vnic_data, attachment_data):
        """
        vnic_data:

        availability_domain (str)       -- The value to assign to the
                                           availability_domain property of
                                           this Vnic.
        compartment_id (str)            -- The value to assign to the
                                           compartment_id property of this Vnic.
        display_name (str)              -- The value to assign to the
                                           display_name property of this Vnic.
        hostname_label (str)            -- The value to assign to the
                                           hostname_label property of this Vnic.
        id (str)                        -- The value to assign to the id
                                           property of this Vnic.
        is_primary (bool)               -- The value to assign to the
                                           is_primary property of this Vnic.
        lifecycle_state (str)           -- The value to assign to the
                                           lifecycle_state property of this
                                           Vnic. Allowed values for this
                                           property are: "PROVISIONING",
                                           "AVAILABLE", "TERMINATING",
                                           "TERMINATED", 'UNKNOWN_ENUM_VALUE'.
                                           Any unrecognized values returned by
                                           a service will be mapped to
                                           'UNKNOWN_ENUM_VALUE'.
        mac_address (str)               -- The value to assign to the
                                           mac_address property of this Vnic.
        private_ip (str)                -- The value to assign to the
                                           private_ip property of this Vnic.
        public_ip (str)                 -- The value to assign to the
                                           public_ip property of this Vnic.
        skip_source_dest_check (bool)   -- The value to assign to the
                                           skip_source_dest_check property
                                           of this Vnic.
        subnet_id (str)                 -- The value to assign to the
                                           subnet_id property of this Vnic.
        time_created (datetime)         -- The value to assign to the
                                           time_created property of this Vnic.


        attachment_data:


        availability_domain (str)       -- The value to assign to the
                                           availability_domain property of
                                           this VnicAttachment.
        compartment_id (str)            -- The value to assign to the
                                           compartment_id property of this
                                           VnicAttachment.
        display_name (str)              -- The value to assign to the
                                           display_name property of this
                                           VnicAttachment.
        id (str)                        -- The value to assign to the id
                                           property of this VnicAttachment.
        instance_id (str)               -- The value to assign to the
                                           instance_id property of this
                                           VnicAttachment.
        lifecycle_state (str)           -- The value to assign to the
                                           lifecycle_state property of this
                                           VnicAttachment. Allowed values for
                                           this property are: "ATTACHING",
                                           "ATTACHED", "DETACHING", "DETACHED",
                                           'UNKNOWN_ENUM_VALUE'. Any
                                           unrecognized values returned by a
                                           service will be mapped to
                                           'UNKNOWN_ENUM_VALUE'.
        nic_index (int)                 -- The value to assign to the
                                           nic_index property of this
                                           VnicAttachment.
        subnet_id (str)                 -- The value to assign to the
                                           subnet_id property of this
                                           VnicAttachment.
        time_created (datetime)         -- The value to assign to the
                                           time_created property of this
                                           VnicAttachment.
        vlan_tag (int)                  -- The value to assign to the vlan_tag
                                           property of this VnicAttachment.
        vnic_id (str)                   -- The value to assign to the vnic_id
                                           property of this VnicAttachment.
        """
        self.oci_session = session
        self.data = vnic_data
        self.att_data = attachment_data
        self.vnic_ocid = vnic_data.id
        self.secondary_private_ips = None

    def __str__(self):
        return "VNIC '%s' (%s)" % (self.data.display_name,
                                   self.vnic_ocid)

    def get_ocid(self):
        return self.vnic_ocid

    def get_state(self):
        return "%s-%s" % (self.data.lifecycle_state, self.att_data.lifecycle_state)

    def get_instance(self):
        return self.oci_session.get_instance(self.att_data.instance_id)

    def refresh(self):
        nc = self.oci_session.get_network_client()
        cc = self.oci_session.get_compute_client()
        try:
            self.data = self.oci_session.sdk_call(
                nc.get_vnic,
                vnic_id=self.vnic_ocid).data
            self.att_data = self.oci_session.sdk_call(
                cc.get_vnic_attachment,
                vnic_attachment_id=self.att_data.id)
        except:
            pass

    def get_private_ip(self):
        return self.data.private_ip

    def get_public_ip(self):
        return self.data.public_ip

    def is_primary(self):
        return self.data.is_primary

    def get_mac_address(self):
        return self.data.mac_address

    def get_subnet(self):
        return self.oci_session.get_subnet(subnet_id=self.data.subnet_id)

    def get_hostname(self):
        return self.data.hostname_label

    def add_private_ip(self, private_ip=None, display_name=None,
                       wait=True):
        '''
        Add a secondary private IP for this VNIC
        '''
        cpid = oci_sdk.core.models.CreatePrivateIpDetails(
            display_name=display_name,
            ip_address=private_ip,
            vnic_id=self.get_ocid())
        nc = self.oci_session.get_network_client()
        try:
            privateIp = self.oci_session.sdk_call(
                nc.create_private_ip,
                cpid)
            return OCIPrivateIP(session=self.oci_session,
                                private_ip_data=privateIp.data)
        except oci_sdk.exceptions.ServiceError as e:
            raise OCISDKError("Failed to add private IP: %s" % e.message)
        # FIXME: wait not implemented!
        return None

    def find_private_ip(self, ip_address, refresh=False):
        '''
        Find a secondary private IP based on its IP address
        '''
        for priv_ip in self.all_private_ips:
            if priv_ip.get_address() == ip_address:
                return priv_ip
        return None

    def all_private_ips(self, refresh=False):
        '''
        return a list of secondary private IPs assigned to this VNIC
        '''
        if self.secondary_private_ips is not None and not refresh:
            return self.secondary_private_ips

        nc = self.oci_session.get_network_client()
        all_privips = []
        try:
            privips = self.oci_session.sdk_call(
                nc.list_private_ips,
                vnic_id=self.get_ocid()).data
        except:
            return []
        for privip in privips:
            if privip.is_primary:
                continue
            all_privips.append(OCIPrivateIP(session=self.oci_session,
                                            private_ip_data=privip))
        self.secondary_private_ips = all_privips
        return all_privips

    def detach(self, wait=True):
        """
        detach and delete the given VNIC
        """
        if self.is_primary():
            raise OCISDKError("Cannot detach the primary VNIC.")

        cc = self.oci_session.get_compute_client()
        try:
            self.oci_session.sdk_call(cc.detach_vnic,
                vnic_attachment_id=self.att_data.id)
        except Exception as e:
            raise OCISDKError("Failed to detach VNIC: %s" % e)

        if wait:
            try:
                vnic_att = self.oci_session.sdk_call(cc.get_vnic_attachment,
                                self.att_data.id).data
                self.att_data = vnic_att
                while vnic_att.lifecycle_state != "DETACHED":
                    sleep(2)
                    vnic_att = self.oci_session.sdk_call(cc.get_vnic_attachment,
                        self.att_data.id).data
                    self.att_data = vnic_att
            except:
                # best effort
                sleep(5)

        return True
class OCIPrivateIP(OCIAPIObject):
    def __init__(self, session, private_ip_data):
        """
        private_ip_data:

        availability_domain                -- The private IP's Availability
                                              Domain.  Example: Uocm:PHX-AD-1

        compartment_id                     -- The OCID of the compartment
                                              containing the private IP.

        defined_tags                       -- Defined tags for this resource.
                                              Each key is predefined and
                                              scoped to a namespace.
                                              Example:
                                            {"Operations": {"CostCenter": "42"}}
                                              type: dict(str, dict(str, object))

        display_name                       -- A user-friendly name. Does not
                                              have to be unique, and it's
                                              changeable. Avoid entering
                                              confidential information.

        freeform_tags                      -- Free-form tags for this resource.
                                              Each tag is a simple key-value
                                              pair with no predefined name,
                                              type, or namespace.
                                              Example: {"Department": "Finance"}
                                              type: dict(str, str)

        hostname_label                     -- The hostname for the private IP.
                                              Used for DNS. The value is the
                                              hostname portion of the private
                                              IP's fully qualified domain name
                                              (FQDN) (for example,
                                              bminstance-1 in FQDN
                                     bminstance-1.subnet123.vcn1.oraclevcn.com).
                                              Must be unique across all VNICs
                                              in the subnet and comply with
                                              RFC 952 and RFC 1123.
                                              Example: bminstance-1

        id                                 -- The private IP's Oracle ID (OCID).

        ip_address                         -- The private IP address of the
                                              privateIp object. The address is
                                              within the CIDR of the VNIC's
                                              subnet.
                                              Example: 10.0.3.3

        is_primary                         -- Whether this private IP is the
                                              primary one on the VNIC. Primary
                                              private IPs are unassigned and
                                              deleted automatically when the
                                              VNIC is terminated.

        subnet_id                          -- The OCID of the subnet the VNIC
                                              is in.  

        time_created                       -- The date and time the private IP
                                              was created, in the format
                                              defined by RFC3339.
                                              Example: 2016-08-25T21:10:29.600Z
 
        vnic_id                            -- The OCID of the VNIC the private
                                              IP is assigned to. The VNIC and
                                              private IP must be in the same
                                              subnet.
    """
        self.oci_session = session
        self.data = private_ip_data
        self.private_ip_ocid = private_ip_data.id
        
    def __str__(self):
        return "Private IP '%s' (%s)" % (self.data.display_name,
                                         self.private_ip_ocid)

    def delete(self):
        '''
        delete this private IP
        Return True for success, False for failure
        '''
        nc = self.oci_session.get_network_client()
        try:
            self.oci_session.sdk_call(
                nc.delete_private_ip,
                self.get_ocid())
            return True
        except:
            return False

    def get_vnic(self):
        return self.oci_session.get_vnic(self.data.vnic_id)

    def get_vnic_ocid(self):
        return self.data.vnic_id

    def get_address(self):
        return self.data.ip_address

    def is_primary(self):
        return self.data.is_primary

    def get_name(self):
        return self.data.display_name

    def get_hostname(self):
        return self.data.hostname_label

    def get_subnet(self):
        return self.oci_session.get_subnet(subnet_id=self.data.subnet_id)

    def get_ocid(self):
        return self.private_ip_ocid

class OCISecurityList(OCIAPIObject):
    protocol = { '1' : 'icmp',
                 '4' : 'ipv4',
                 '6' : 'tcp',
                 '17' :'udp' }
    def __init__(self, session, security_list_data):
        """

        :param session:
        :param security_list_data:

        compartment-id (str)    -- compartment ocid
        defined-tags ()         --
        display-name (str)      -- name assigned to the list

        egress-security-rules[]   -- list of egress rules, each has the following properties:

            protocol(all)
            destination("0.0.0.0/0")
            icmp-options(null)
            is-stateless(bool)
            tcp-options(null),
            udp-options(null)

        freeform-tags{dict(str, str)}

        id                         -- ocid1.securitylist.oc1...,

        ingress-security-rules[]   -- list of ingress rules, each has the follwoing properties:
            protocol(all)          -- choice of all, 17(UDP),6(TCP), 1(ICMP), etc
            source("0.0.0.0/0")
            icmp-options(
                code: null,
                type: 3
            )
            is-stateless(bool)
            tcp-options(
                destination-port-range: {
                max: 22,
              \ min: 22
            }
            "source-port-range": null
          },

            udp-options(null)

        lifecycle-state (str)       -- choice"PROVISIONING",
                                        "AVAILABLE", "TERMINATING",
                                        "TERMINATED", 'UNKNOWN_ENUM_VALUE'.
                                        Any unrecognized values returned

        time-created(datetime)      -- 2018-01-12T17:44:05.706000+00:00

        vcn-id              -- ocid1.vcn.oc1...

        """
        self.security_list_ocid = security_list_data.id
        self.oci_session = session
        self.data = security_list_data

    def get_display_name(self):
        return self.data.display_name

    def get_ingress_rules(self):
        return self.data.ingress_security_rules

    def get_egress_rules(self):
        return self.data.egress_security_rules

    def print_security_list(self, indent):
        print "%sSecurity List: %s" % (indent, self.get_display_name())
        for rule in self.get_ingress_rules():
            prot = OCISecurityList.protocol.get(rule.protocol, rule.protocol)
            src = rule.source
            des = "---"
            desport = "-"
            srcport = "-"
            if rule.protocol == "6" or rule.protocol == "17":
                if rule.protocol == "6":
                    option = rule.tcp_options
                else:
                    option = rule.udp_options

                try:
                    if option.destination_port_range.min != option.destination_port_range.max:
                        desport = "%s-%s" % (option.destination_port_range.min, option.destination_port_range.max)
                    else:
                        desport = option.destination_port_range.min
                except:
                    pass

                try:
                    if option.source_port_range.min != option.source_port_range.max:
                        srcport = "%s-%s" % (option.source_port_range.min, option.source_port_range.max)
                    else:
                        srcport = option.source_port_range.min
                except:
                    pass
            elif rule.protocol == "1":
                srcport = "-"
                option = rule.icmp_options
                desport = "type--"
                try:
                    desport = "type-%s" %  option.type
                except:
                    pass
                try:
                    des = "code-%s" %  option.code
                except:
                    des = "code--"
            print "%s  Ingress: %-5s %20s:%-6s %20s:%s" % (indent, prot, src, srcport, des, desport)

        for rule in self.get_egress_rules():
            prot = OCISecurityList.protocol.get(rule.protocol, rule.protocol)
            des = rule.destination
            src = "---"
            if rule.protocol == 6 or rule.protocol == 17:
                if rule.protocol == 6:
                    option = rule.tcp_options
                else:
                    option = rule.udp_options

                try:
                    if option.destination_port_range.min != option.destination_port_range.max:
                        desport = "%s-%s" % (option.destination_port_range.min,option.destination_port_range.max)
                    else:
                        desport = option.destination_port_range.min
                except:
                    desport = "-"

                try:
                    if option.source_port_range.min != option.source_port_range.max:
                        srcport = "%s-%s" % (option.source_port_range.min,option.source_port_range.max)
                    else:
                        srcport = option.source_port_range.min

                except:
                    srcport = "-"
            elif rule.protocol == 1:
                srcport = "-"
                option = rule.icmp_options
                try:
                    desport = "type-%s" %  option.type
                except:
                    desport = "type--"
                try:
                    des = "code-%s" %  option.code
                except:
                    des = "code--"
            print "%s  Egress : %-5s %20s:%-6s %20s:%s" % (indent, prot, src, srcport, des, desport)


class OCISubnet(OCIAPIObject):
    def __init__(self, session, subnet_data):
        """
        subnet_data:

        availability_domain (str)         -- The value to assign to the
                                             availability_domain property
                                             of this Subnet.
        cidr_block (str)                  -- The value to assign to the
                                             cidr_block property of this Subnet.
        compartment_id (str)              -- The value to assign to the
                                             compartment_id property of this
                                             Subnet.
        defined_tags (dict(str, dict(str, object))) -- The value to assign to
                                             the defined_tags property of this
                                             Subnet.
        dhcp_options_id (str)             -- The value to assign to the
                                             dhcp_options_id property of this
                                             Subnet.
        display_name (str)                -- The value to assign to the
                                             display_name property of this
                                             Subnet.
        dns_label (str)                   -- The value to assign to the
                                             dns_label property of this Subnet.
        freeform_tags (dict(str, str))    -- The value to assign to the
                                             freeform_tags property of this
                                             Subnet.
        id (str)                          -- The value to assign to the id
                                             property of this Subnet.
        lifecycle_state (str)             -- The value to assign to the
                                             lifecycle_state property of this
                                             Subnet. Allowed values for this
                                             property are: "PROVISIONING",
                                             "AVAILABLE", "TERMINATING",
                                             "TERMINATED", 'UNKNOWN_ENUM_VALUE'.
                                             Any unrecognized values returned
                                             by a service will be mapped to
                                             'UNKNOWN_ENUM_VALUE'.
        prohibit_public_ip_on_vnic (bool) -- The value to assign to the
                                             prohibit_public_ip_on_vnic
                                             property of this Subnet.
        route_table_id (str)              -- The value to assign to the
                                             route_table_id property of this
                                             Subnet.
        security_list_ids (list[str])     -- The value to assign to the
                                             security_list_ids property of
                                             this Subnet.
        subnet_domain_name (str)          -- The value to assign to the
                                             subnet_domain_name property of
                                             this Subnet.
        time_created (datetime)           -- The value to assign to the
                                             time_created property of this
                                             Subnet.
        vcn_id (str)                      -- The value to assign to the vcn_id
                                             property of this Subnet.
        virtual_router_ip (str)           -- The value to assign to the
                                             virtual_router_ip property of
                                             this Subnet.
        virtual_router_mac (str)          -- The value to assign to the
                                             virtual_router_mac property of
                                             this Subnet.
        """
        self.oci_session = session
        self.data = subnet_data
        self.vnics = None
        self.subnet_ocid = subnet_data.id
        self.secondary_private_ips = None

    def __str__(self):
        return "Subnet '%s' (%s)" % (self.data.display_name,
                                     self.subnet_ocid)

    def get_ocid(self):
        return self.subnet_ocid

    def get_cidr_block(self):
        return self.data.cidr_block

    def get_vcn_id(self):
        return self.data.vcn_id

    def get_security_list_ids(self):
        return self.data.security_list_ids


    def all_vnics(self, refresh=False):
        """
        return a list of all OCIVNIC objects that are in this subnet
        """
        if self.vnics is not None and len(self.vnics) > 0 and not refresh:
            return self.vnics
        compartment = self.oci_session.get_compartment(self.data.compartment_id)
        if compartment is None:
            return []
        vnics = []
        for vnic in compartment.all_vnics(refresh=refresh):
            if vnic.data.subnet_id == self.subnet_ocid:
                vnics.append(vnic)

        self.vnics = vnics
        return vnics

    def ip_matches(self, ipaddr):
        """
        Verify if the given IP address matches the cidr block of the subnet.
        Return True of it does, False if it doesn't.
        """
        match = re.match(r'([0-9]+)\.([0-9]+)\.([0-9]+)\.([0-9]+)',
                         ipaddr)
        if match is None:
            raise OCISDKError('Failed to parse IP address %s' % \
                              ipaddr)
        if int(match.group(1)) > 255 or \
           int(match.group(2)) > 255 or \
           int(match.group(3)) > 255 or \
           int(match.group(4)) > 255:
            raise OCISDKError('Invalid IP address: %s' % ipaddr)
        ipint = int(match.group(1))*(256**3) + \
                   int(match.group(2))*(256**2) + \
                   int(match.group(3))*256 + \
                   int(match.group(4))
        match = re.match(r'([0-9]+)\.([0-9]+)\.([0-9]+)\.([0-9]+)/([0-9]+)',
                         self.data.cidr_block)
        if match is None:
            raise OCISDKError('Failed to parse cidr block %s' % \
                              self.data.cidr_block)
        cidripint = int(match.group(1))*(256**3) + \
                    int(match.group(2))*(256**2) + \
                    int(match.group(3))*256 + \
                    int(match.group(4))
        cidrmask = int("1" * int(match.group(5)) + \
                       "0" * (32-int(match.group(5))),\
                       2)
        return ((ipint & cidrmask) == cidripint)

    def all_private_ips(self, refresh=False):
        '''
        return a list of secondary private IPs in this Subnet
        '''
        if self.secondary_private_ips is not None and not refresh:
            return self.secondary_private_ips

        nc = self.oci_session.get_network_client()
        all_privips = []
        try:
            privips = self.oci_session.sdk_call(
                nc.list_private_ips,
                subnet_id=self.get_ocid()).data
        except:
            return []
        for privip in privips:
            if privip.is_primary:
                continue
            all_privips.append(OCIPrivateIP(session=self.oci_session,
                                            private_ip_data=privip))
        self.secondary_private_ips = all_privips
        return all_privips

    def all_private_ips_with_primary(self, refresh=False):
        '''
        return a list of secondary private IPs in this Subnet
        '''
        if self.secondary_private_ips is not None and not refresh:
            return self.secondary_private_ips

        nc = self.oci_session.get_network_client()
        all_privips = []
        try:
            privips = self.oci_session.sdk_call(
                nc.list_private_ips,
                subnet_id=self.get_ocid()).data
        except:
            return []
        for privip in privips:
            all_privips.append(OCIPrivateIP(session=self.oci_session,
                                            private_ip_data=privip))
        self.secondary_private_ips = all_privips
        return all_privips

class OCIVolume(OCIAPIObject):
    def __init__(self, session, volume_data, attachment_data=None):
        """
        volume_data:

        availability_domain (str)          -- The value to assign to the
                                              availability_domain property of
                                              this Volume.
        compartment_id (str)               -- The value to assign to the
                                              compartment_id property of this
                                              Volume.
        defined_tags (dict(str, dict(str, object))) -- The value to assign to
                                              the defined_tags property of
                                              this Volume.
        display_name (str)                 -- The value to assign to the
                                              display_name property of this
                                              Volume.
        freeform_tags (dict(str, str))     -- The value to assign to the
                                              freeform_tags property of this
                                              Volume.
        id (str)                           -- The value to assign to the id
                                              property of this Volume.
        is_hydrated (bool)                 -- The value to assign to the
                                              is_hydrated property of this
                                              Volume.
        lifecycle_state (str)              -- The value to assign to the
                                              lifecycle_state property of this
                                              Volume. Allowed values for this
                                              property are: "PROVISIONING",
                                              "RESTORING", "AVAILABLE",
                                              "TERMINATING", "TERMINATED",
                                              "FAULTY", 'UNKNOWN_ENUM_VALUE'.
                                              Any unrecognized values returned
                                              by a service will be mapped to
                                              'UNKNOWN_ENUM_VALUE'.
        size_in_gbs (int)                  -- The value to assign to the
                                              size_in_gbs property of this
                                              Volume.
        size_in_mbs (int)                  -- The value to assign to the
                                              size_in_mbs property of this
                                              Volume.
        source_details (VolumeSourceDetails) -- The value to assign to the
                                              source_details property of this
                                              Volume.
        time_created (datetime)            -- The value to assign to the
                                              time_created property of this
                                              Volume.

        attachment_data:

        
        attachment_type (str)              -- The value to assign to the
                                              attachment_type property of this
                                              VolumeAttachment.
        availability_domain (str)          -- The value to assign to the
                                              availability_domain property of
                                              this VolumeAttachment.
        compartment_id (str)               -- The value to assign to the
                                              compartment_id property of this
                                              VolumeAttachment.
        display_name (str)                 -- The value to assign to the
                                              display_name property of this
                                              VolumeAttachment.
        id (str)                           -- The value to assign to the id
                                              property of this VolumeAttachment.
        instance_id (str)                  -- The value to assign to the
                                              instance_id property of this
                                              VolumeAttachment.
        lifecycle_state (str)              -- The value to assign to the
                                              lifecycle_state property of this
                                              VolumeAttachment. Allowed values
                                              for this property are:
                                              "ATTACHING", "ATTACHED",
                                              "DETACHING", "DETACHED",
                                              'UNKNOWN_ENUM_VALUE'.
                                              Any unrecognized values returned
                                              by a service will be mapped to
                                              'UNKNOWN_ENUM_VALUE'.
        time_created (datetime)            -- The value to assign to the
                                              time_created property of this
                                              VolumeAttachment.
        volume_id (str)                    -- The value to assign to the
                                              volume_id property of this
                                              VolumeAttachment.

        """
        self.oci_session = session
        self.data = volume_data
        self.att_data = attachment_data
        self.volume_ocid = volume_data.id
        self.HUMAN = 'HUMAN'
        self.GB = 'GB'
        self.MB = 'MB'

    def __str__(self):
        return "Volume '%s' (%s)" % (self.data.display_name,
                                     self.volume_ocid)

    def get_ocid(self):
        return self.volume_ocid

    def set_volume_attachment(self, attachment_data):
        self.att_data = attachment_data

    def unset_volume_attachment(self, attachment_data):
        # volume is not attached
        self.att_data = None

    def get_attachment_state(self):
        if self.att_data is None:
            return 'NOT_ATTACHED'

        return self.att_data.lifecycle_state

    def is_attached(self):
        if self.att_data is None:
            return False

        return self.att_data.lifecycle_state == 'ATTACHED'

    def get_size(self, format=None):
        '''
        Return the size of the volume in the chosen format.
        Default: self.HUMAN
        Other options: self.GB (Gigabytes), self.MB (Megabytes)
        self.HUMAN is a string, the other 2 formats are ints
        '''
        if format == self.GB:
            return self.data.size_in_gbs
        elif format == self.MB:
            return self.data.size_in_mbs
        else:
            return str(self.data.size_in_gbs) + 'GB'

    def get_user(self):
        if self.att_data is None:
            return None

        try:
            return self.att_data.chap_username
        except:
            return None

    def get_password(self):
        if self.att_data is None:
            return None

        try:
            return self.att_data.chap_secret
        except:
            return None

    def get_portal_ip(self):
        if self.att_data is None:
            return None

        try:
            return self.att_data.ipv4
        except:
            return None

    def get_portal_port(self):
        if self.att_data is None:
            return None

        try:
            return self.att_data.port
        except:
            return None

    def get_instance(self):
        if self.att_data is None:
            return None

        try:
            return self.oci_session.get_instance(self.att_data.instance_id)
        except:
            return None

    def get_iqn(self):
        if self.att_data is None:
            return None

        try:
            return self.att_data.iqn
        except:
            return None

    def attach_to(self, instance_id, use_chap=False,
                  display_name=None, wait=True):
        """
        attach the volume to the given instance
        """
        av_det = oci_sdk.core.models.AttachIScsiVolumeDetails(
            type="iscsi",
            use_chap=use_chap,
            volume_id=self.get_ocid(),
            instance_id=instance_id,
            display_name=display_name
            )
        cc = self.oci_session.get_compute_client()
        try:
            vol_att = self.oci_session.sdk_call(
                cc.attach_volume,
                av_det)
            if wait:
                while vol_att.data.lifecycle_state != "ATTACHED":
                    sleep(2)
                    vol_att = self.oci_session.sdk_call(
                        cc.get_volume_attachment,
                        vol_att.data.id)
            return self.oci_session.get_volume(vol_att.data.volume_id,
                                               refresh=True)
        except oci_sdk.exceptions.ServiceError as e:
            raise OCISDKError('Failed to attach volume: %s' % e.message)

    def detach(self, wait=True):
        if not self.is_attached():
            return True

        cc = self.oci_session.get_compute_client()
        
        try:
            self.oci_session.sdk_call(
                cc.detach_volume,
                volume_attachment_id=self.att_data.id)
        except oci_sdk.exceptions.ServiceError as e:
            raise OCISDKError('Failed to detach volume: %s' % e.message)

        if wait:
            try:
                vol_att = self.oci_session.sdk_call(
                    cc.get_volume_attachment,
                    self.att_data.id).data
            except:
                # can't verify if detachment finished for some reason
                # best effort: sleep a bit and return
                sleep(10)
                return True
            while vol_att.lifecycle_state != "DETACHED":
                sleep(2)
                try:
                    vol_att = self.oci_session.sdk_call(
                        cc.get_volume_attachment,
                        self.att_data.id).data
                    self.att_data = vol_att
                except:
                    sleep(10)
                    return True
        return True

    def destroy(self):
        """
        Destroy the volume.
        """
        if self.is_attached():
            raise OCISDKError("Volume is currently attached, cannot destroy.")

        bsc = self.oci_session.get_block_storage_client()
        try:
            self.oci_session.sdk_call(bsc.delete_volume,
                volume_id=self.volume_ocid)
        except Exception as e:
            raise OCISDKError("Failed to destroy volume: %s" % e)
