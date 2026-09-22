"""Explicit, tiny paid connectivity check with synthetic text only; no business data."""
import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ostag.briefs.deepseek_credentials import read_key
from ostag.briefs.deepseek_profile import generation_config
from ostag.briefs.deepseek_transport import DeepSeekReadOnlyTransport, DeepSeekTransportError
from ostag.briefs.generation import TransportRequest


async def check(directory):
    parameters = generation_config().parameters
    parameters = replace(parameters, budget=replace(parameters.budget,
        max_input_bytes=2048, max_output_tokens=128, max_output_bytes=1024,
        timeout_seconds=20))
    now = datetime.now(timezone.utc)
    result = await DeepSeekReadOnlyTransport(api_key=read_key(directory)).generate(
        TransportRequest(serialized_input='Return a json object with ok set to true.',
            parameters=parameters, max_output_bytes=1024, remaining_seconds=20,
            cancellation=asyncio.Event(), charged_day=now.strftime('%Y-%m-%d'),
            deadline=(now + timedelta(seconds=20)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            reauthorize=lambda: None))
    import json
    value = json.loads(result.body)
    if result.usage is None or type(value) is not dict or value.get('ok') is not True:
        raise ValueError('connectivity check failed')
    return result.usage.cost_micros


def main():
    parser = argparse.ArgumentParser(description='Small paid synthetic DeepSeek connectivity check.')
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    try:
        micros = asyncio.run(check(args.directory))
    except DeepSeekTransportError as error:
        # Exact adapter class emits only a fixed diagnostic allowlist.
        print('Connection check failed: ' + str(error))
        return 1
    except (Exception, KeyboardInterrupt):
        print('Connection check failed. Key and raw provider response were not printed.')
        return 1
    print(f'DeepSeek connection verified. Conservative cost: {micros} microUSD. '
          'No business data sent; production activation still requires rollout checks.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
