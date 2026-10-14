import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud.runner import Bridge, BridgeRequestError
from cloud.runner import run
from unittest.mock import Mock


class BridgeRetryTests(unittest.TestCase):
    def bridge(self):
        return Bridge('https://script.google.com/macros/s/test/exec', 'x' * 40,
                      '9d5db4ef-5c77-4e4c-a461-165c239545ec', '123:1')

    def response(self, value):
        return io.BytesIO(json.dumps(value).encode())

    def test_busy_retries_same_action_with_fresh_envelope(self):
        replies = [self.response({'ok': False, 'code': 'BUSY', 'retryable': True}),
                   self.response({'ok': True, 'value': {'stopRequested': True}})]
        with patch('cloud.runner.urlopen', side_effect=replies) as send, patch('cloud.runner.time.sleep'):
            result = self.bridge().call('progress', progress={'completed': 14})
        self.assertTrue(result['stopRequested'])
        requests = [json.loads(call.args[0].data) for call in send.call_args_list]
        self.assertEqual(requests[0]['payload'], requests[1]['payload'])
        self.assertNotEqual(requests[0]['nonce'], requests[1]['nonce'])
        self.assertNotEqual(requests[0]['signature'], requests[1]['signature'])

    def test_auth_lease_terminal_and_unknown_errors_never_retry(self):
        for code in ['AUTH_REJECTED', 'INVALID_LEASE', 'JOB_NOT_ACTIVE', 'REPLAY_REJECTED', 'INTERNAL_ERROR']:
            with self.subTest(code=code), patch('cloud.runner.urlopen', return_value=self.response(
                    {'ok': False, 'code': code, 'retryable': True})) as send, patch('cloud.runner.time.sleep') as sleep:
                with self.assertRaisesRegex(BridgeRequestError, code):
                    self.bridge().call('progress')
                self.assertEqual(send.call_count, 1)
                sleep.assert_not_called()

    def test_temporary_service_error_stops_after_bounded_attempts(self):
        with patch('cloud.runner.urlopen', side_effect=lambda *a, **k: self.response(
                {'ok': False, 'code': 'SERVICE_TEMPORARY', 'retryable': True})) as send, patch('cloud.runner.time.sleep'):
            with self.assertRaisesRegex(BridgeRequestError, 'SERVICE_TEMPORARY'):
                self.bridge().call('progress')
            self.assertEqual(send.call_count, 5)

    def test_http_authorization_failure_does_not_retry(self):
        with patch('cloud.runner.urlopen', side_effect=HTTPError('test', 403, '', {}, None)) as send:
            with self.assertRaisesRegex(BridgeRequestError, '403'):
                self.bridge().call('claim')
            self.assertEqual(send.call_count, 1)

    def test_timeout_retries_commit_not_collection(self):
        with patch('cloud.runner.urlopen', side_effect=[TimeoutError(), self.response(
                {'ok': True, 'value': {'saved': True}})]) as send, patch('cloud.runner.time.sleep'):
            self.assertTrue(self.bridge().call('commit', bundle='test')['saved'])
            self.assertEqual(send.call_count, 2)

    def test_content_service_404_reposts_to_original_url_with_fresh_nonce(self):
        for action in ['claim', 'progress', 'commit']:
            with self.subTest(action=action), patch('cloud.runner.urlopen', side_effect=[
                    HTTPError('https://script.googleusercontent.com/macros/echo?user_content_key=private',
                              404, '', {}, None),
                    self.response({'ok': True, 'value': {'saved': True}})]) as send, patch(
                    'cloud.runner.time.sleep'):
                bridge = self.bridge()
                self.assertTrue(bridge.call(action)['saved'])
                self.assertEqual(send.call_count, 2)
                requests = [call.args[0] for call in send.call_args_list]
                self.assertTrue(all(request.full_url == bridge.url for request in requests))
                bodies = [json.loads(request.data) for request in requests]
                self.assertEqual(bodies[0]['payload'], bodies[1]['payload'])
                self.assertNotEqual(bodies[0]['nonce'], bodies[1]['nonce'])

    def test_non_content_service_404_is_not_retried(self):
        for url in ['https://script.google.com/macros/s/test/exec',
                    'https://script.googleusercontent.com/other',
                    'https://script.googleusercontent.com.example.com/macros/echo',
                    'http://script.googleusercontent.com/macros/echo']:
            with self.subTest(url=url), patch('cloud.runner.urlopen', side_effect=HTTPError(
                    url, 404, '', {}, None)) as send, patch('cloud.runner.time.sleep') as sleep:
                with self.assertRaisesRegex(BridgeRequestError, '404'):
                    self.bridge().call('claim')
                self.assertEqual(send.call_count, 1)
                sleep.assert_not_called()

    def test_content_service_404_retries_are_bounded_and_do_not_log_response_url(self):
        url = 'https://script.googleusercontent.com/macros/echo?user_content_key=private'
        with patch('cloud.runner.urlopen', side_effect=HTTPError(url, 404, '', {}, None)) as send, patch(
                'cloud.runner.time.sleep'), patch('builtins.print') as log:
            with self.assertRaises(BridgeRequestError) as caught:
                self.bridge().call('progress')
            self.assertEqual(send.call_count, 5)
            self.assertEqual(caught.exception.code, 'RETRY_EXHAUSTED')
            self.assertNotIn('private', str(caught.exception))
            self.assertNotIn('private', str(log.call_args_list))
            self.assertIn('RESPONSE_URL_EXPIRED', str(log.call_args_list))

    def test_bundle_transfer_has_longer_timeout_than_heartbeat(self):
        for action, expected in [('claim', 100), ('commit', 100), ('progress', 40)]:
            with patch('cloud.runner.time.monotonic', return_value=0), patch(
                    'cloud.runner.urlopen', return_value=self.response({'ok': True, 'value': {}})) as send:
                self.bridge().call(action)
                self.assertEqual(send.call_args.kwargs['timeout'], expected)

    def test_deadline_prevents_heartbeat_expiry(self):
        with patch('cloud.runner.time.monotonic', side_effect=[0, 0, 209]), patch(
                'cloud.runner.urlopen', side_effect=TimeoutError()) as send, patch('cloud.runner.time.sleep') as sleep:
            with self.assertRaises(BridgeRequestError):
                self.bridge().call('progress')
            self.assertEqual(send.call_count, 1)
            sleep.assert_not_called()

    def test_startup_recovers_after_the_old_five_attempt_limit(self):
        replies = [io.BytesIO(b'<html>temporary response</html>') for _ in range(6)]
        replies.append(self.response({'ok': True, 'value': {'lease': 'same-lease'}}))
        with patch('cloud.runner.urlopen', side_effect=replies) as send, patch('cloud.runner.time.sleep'):
            self.assertEqual(self.bridge().call('claim')['lease'], 'same-lease')
            self.assertEqual(send.call_count, 7)

    def test_startup_is_bounded_and_preserves_last_cause(self):
        with patch('cloud.runner.urlopen', side_effect=TimeoutError()) as send, patch('cloud.runner.time.sleep'):
            with self.assertRaises(BridgeRequestError) as caught:
                self.bridge().call('claim')
            self.assertEqual(send.call_count, 12)
            self.assertEqual(caught.exception.cause_code, 'TIMEOUT')
        with patch('cloud.runner.time.monotonic', side_effect=[0, 0, 479]), patch(
                'cloud.runner.urlopen', side_effect=TimeoutError()) as send, patch('cloud.runner.time.sleep') as sleep:
            with self.assertRaises(BridgeRequestError):
                self.bridge().call('claim')
            self.assertEqual(send.call_count, 1)
            sleep.assert_not_called()

    def test_startup_failure_is_reported_without_lease_or_collection(self):
        error = BridgeRequestError('fixture', 'RETRY_EXHAUSTED', 'claim', 'INVALID_JSON')
        bridge = Mock()
        bridge.call.side_effect = [error, {'recorded': True}]
        with patch('cloud.runner.execute') as execute:
            with self.assertRaises(BridgeRequestError):
                run(bridge)
            execute.assert_not_called()
        self.assertEqual(bridge.call.call_args_list[1].args, ('startup_fail',))
        self.assertEqual(bridge.call.call_args_list[1].kwargs, {'code': 'INVALID_JSON'})

    def test_failure_reporting_does_not_hide_original_error_or_loop(self):
        error = BridgeRequestError('fixture', 'RETRY_EXHAUSTED', 'claim', 'TIMEOUT')
        bridge = Mock()
        bridge.call.side_effect = [error, RuntimeError('reporting unavailable')]
        with self.assertRaises(BridgeRequestError) as caught:
            run(bridge)
        self.assertIs(caught.exception, error)
        self.assertEqual(bridge.call.call_count, 2)


if __name__ == '__main__':
    unittest.main()
