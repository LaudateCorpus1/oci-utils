#!/usr/bin/python

import unittest
import oci_utils
import oci_utils.iscsiadm

# skip tests that require an OCI instance if not running on an OCI instance
def skipUnlessOCI():
    if not oci_utils.iscsiadm.__can_connect('169.254.169.254', 80):
        return unittest.skip("must be run on an OCI instance")
    return lambda func: func

class TestOciMetadata(unittest.TestCase):
    @skipUnlessOCI()
    def test__oci_metadata__get(self):
        metadata = oci_utils.metadata().get()
        self.assertNotEqual(metadata, [])
        self.assertTrue(metadata['instance'])
        self.assertIn(u'region', metadata['instance'])
        self.assertIn(metadata['instance']['region'], ['phx','iad','fra','lhr'])
        self.assertIn(u'state', metadata['instance'])
        self.assertEquals(metadata['instance']['state'], 'Running')

    @skipUnlessOCI()
    def test__oci_metadata__filter(self):
        metadata = oci_utils.metadata().filter(['macaddr', 'instance'])
        self.assertTrue(metadata)
        self.assertIn(u'instance', metadata)
        self.assertIn(u'compartmentId', metadata['instance'])
        self.assertIn('ocid1.compartment.oc1..',
                      metadata['instance']['compartmentId'])
        self.assertIn(u'vnics', metadata)
        self.assertIn(u'macAddr', metadata['vnics'][0])
        self.assertNotIn(u'vnicId', metadata['vnics'][0])

if __name__ == '__main__':
    suite = unittest.TestLoader().loadTestsFromTestCase(TestOciMetadata)
    unittest.TextTestRunner(verbosity=2).run(suite)
