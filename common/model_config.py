"""
Model / provider configuration (models.json, Section VIII).

models.json lists, for each provider, its OpenAI-compatible base URL, the
name of the environment variable holding its API key(s), and the models we
use with it. The keys themselves never go in this file (Section VI.3).

resolve_model() turns the CLI arguments plus models.json into the three
values LLMProvider needs. The explicit --provider-url path works exactly as
before and does not need models.json at all, so the evaluation's
`--model-name X --provider-url Y` call keeps working unchanged.

The provider is never guessed from the model name: OpenRouter model ids
contain "/" too ("openrouter/free", "google/gemma-..."), so "x/y" cannot be
told apart from "<provider>/<model>".
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

DEFAULT_MODELS_CONFIG = "models.json"

# The key variable used before models.json existed; kept as the last resort
# so a bare `--model-name X --provider-url Y` call behaves as it always did.
FALLBACK_API_KEY_ENV = "OPENROUTER_API_KEY"


class ProviderConfig(BaseModel):
    base_url: str
    api_key_env: str
    # For reference only: a model missing from this list is still used as-is,
    # since free model ids change often.
    models: List[str] = Field(default_factory=list)


class DefaultModel(BaseModel):
    provider: str
    model: str


class ModelsConfig(BaseModel):
    default: Optional[DefaultModel] = None
    providers: Dict[str, ProviderConfig] = Field(default_factory=dict)


def load_models_config(path: str) -> Optional[ModelsConfig]:
    """None if the file does not exist; raises if it exists but is invalid."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return ModelsConfig(**json.load(f))


def _same_url(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")


def resolve_model(
    config_path: str = DEFAULT_MODELS_CONFIG,
    provider: Optional[str] = None,
    model_name: Optional[str] = None,
    provider_url: Optional[str] = None,
    api_key_env: Optional[str] = None,
) -> Tuple[str, str, str]:
    """Return (model_name, base_url, api_key_env) for LLMProvider.

    - --provider-url given: use it as-is (needs --model-name). The key
      variable is --api-key-env if given, else the one of the provider in
      models.json with the same base URL, else OPENROUTER_API_KEY.
    - --provider given: base URL and key variable come from models.json;
      the model is --model-name, or that provider's first listed model.
    - neither: the "default" entry of models.json.
    """
    config = load_models_config(config_path)

    if provider_url:
        if not model_name:
            raise ValueError("--model-name is required together with --provider-url")
        if api_key_env:
            return model_name, provider_url, api_key_env
        if config is not None:
            for p in config.providers.values():
                if _same_url(p.base_url, provider_url):
                    return model_name, provider_url, p.api_key_env
        return model_name, provider_url, FALLBACK_API_KEY_ENV

    if config is None:
        raise ValueError(
            f"No --provider-url given and no model config found at {config_path!r}"
        )

    if provider is None:
        if config.default is None:
            raise ValueError(
                f"No --provider/--provider-url given and {config_path} has no 'default'"
            )
        provider = config.default.provider
        model_name = model_name or config.default.model

    if provider not in config.providers:
        available = ", ".join(sorted(config.providers)) or "(none)"
        raise ValueError(
            f"Unknown provider {provider!r} in {config_path}; available: {available}"
        )
    p = config.providers[provider]

    if not model_name:
        if not p.models:
            raise ValueError(
                f"No --model-name given and provider {provider!r} lists no models in {config_path}"
            )
        model_name = p.models[0]

    return model_name, p.base_url, api_key_env or p.api_key_env
