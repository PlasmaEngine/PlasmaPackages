import json
import unittest
from unittest.mock import patch

import publish_package
import render_index


class CataloguePaginationTests(unittest.TestCase):
    def test_duplicate_lookup_checks_later_pages(self):
        existing = {'Key': 'plasma.jolt@1.2.0'}
        with patch.object(publish_package, 'api', side_effect=[
            {'results': [{'Key': 'older'}], 'next': 'http://catalogue/page=2'},
            {'results': [existing], 'next': None},
        ]) as api:
            self.assertEqual(publish_package.find_row({}, 850, 'token', 'Key', existing['Key']), existing)
            self.assertIn('page=2', api.call_args.args[2])

    def test_renderer_keeps_configured_https_host_when_paginating(self):
        class Response:
            def __init__(self, data):
                self.data = data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return json.dumps(self.data).encode()

        with patch.object(render_index.urllib.request, 'urlopen', side_effect=[
            Response({'results': [{'id': 1}], 'next': 'http://catalogue/next'}),
            Response({'results': [{'id': 2}], 'next': None}),
        ]) as request:
            rows = render_index.baserow_rows({'baserow': {'baseUrl': 'https://catalogue'}}, 850, 'token')
            self.assertEqual(rows, [{'id': 1}, {'id': 2}])
            self.assertEqual(request.call_args.args[0].full_url,
                             'https://catalogue/api/database/rows/table/850/?user_field_names=true&size=200&page=2')


if __name__ == '__main__':
    unittest.main()
