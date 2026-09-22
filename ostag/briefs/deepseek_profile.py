"""Explicit DeepSeek composition inputs; never enable features or create grants."""
from pathlib import Path

from ostag.briefs.deepseek_credentials import read_key
from ostag.briefs.deepseek_transport import DeepSeekReadOnlyTransport, MODEL_ID
from ostag.briefs.generation import Budget, GenerationConfig, Parameters


def generation_config() -> GenerationConfig:
    # Up to $0.10 per job, $1 per UTC day; integer admission rates round up.
    # The transport settles measured tokens at the conservative peak USD tariff.
    return GenerationConfig(Parameters(MODEL_ID, Budget(
        max_job_micros=100000, max_input_bytes=64000, max_output_bytes=32000,
        max_output_tokens=2048, input_micros_per_token=1, output_micros_per_token=2,
        max_calls=1, timeout_seconds=120)), daily_limit_micros=1000000)


def production_inputs(secret_directory: Path):
    """Host factory uses these fields in its authenticated TrustedBriefComposition."""
    return dict(model_transport=DeepSeekReadOnlyTransport(api_key=read_key(secret_directory)),
                generation=generation_config())
