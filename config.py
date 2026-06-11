"""
config.py — Application configuration persistence (API keys, etc.)
"""
APP_VERSION = "1.0.1"
UPDATE_CHECK_URL = "https://api.github.com/repos/caokai1073/Rivus/releases/latest"

import json
import os
from pathlib import Path

_data_dir = os.environ.get("MEMORYVAULT_DATA_DIR")
CONFIG_PATH = (
    Path(_data_dir) / "config.json" if _data_dir
    else Path(__file__).parent / "config.json"
)

# Cloud provider metadata
CLOUD_PROVIDERS = {
    "openai": {
        "name": "OpenAI / ChatGPT",
        "base_url": "https://api.openai.com/v1",
        "models": [
            {"id": "gpt-5.5",      "label": "GPT-5.5 · Flagship"},
            {"id": "gpt-5.4",      "label": "GPT-5.4"},
            {"id": "gpt-5.4-mini", "label": "GPT-5.4 mini"},
            {"id": "gpt-4.1",      "label": "GPT-4.1"},
            {"id": "o3",           "label": "o3 · Reasoning"},
        ],
    },
    "anthropic": {
        "name": "Anthropic / Claude",
        "base_url": "https://api.anthropic.com",
        "models": [
            {"id": "claude-fable-5",           "label": "Claude Fable 5 · Flagship"},
            {"id": "claude-opus-4-8",          "label": "Claude Opus 4.8"},
            {"id": "claude-sonnet-4-6",        "label": "Claude Sonnet 4.6"},
            {"id": "claude-haiku-4-5-20251001","label": "Claude Haiku 4.5"},
        ],
    },
    "qwen": {
        "name": "Qwen / 通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": [
            {"id": "qwen3-235b-a22b", "label": "Qwen3 235B · Flagship"},
            {"id": "qwen3-72b",       "label": "Qwen3 72B"},
            {"id": "qwen-max",        "label": "Qwen Max"},
            {"id": "qwen-plus",       "label": "Qwen Plus"},
            {"id": "qwen-turbo",      "label": "Qwen Turbo"},
        ],
    },
    "nvidia": {
        "name": "NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "models": [
            {"id": "nvidia/llama-3.1-nemotron-ultra-253b-v1", "label": "Nemotron Ultra 253B · Flagship"},
            {"id": "meta/llama-3.3-70b-instruct",             "label": "Llama 3.3 70B"},
            {"id": "mistralai/mistral-large-2-instruct",      "label": "Mistral Large 2"},
        ],
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": [
            {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash"},
            {"id": "deepseek-v4-pro",   "label": "DeepSeek V4 Pro"},
        ],
    },
    "minimax": {
        "name": "MiniMax",
        "base_url": "https://api.minimax.chat/v1",
        "models": [
            {"id": "MiniMax-M1",       "label": "MiniMax M1 · Flagship"},
            {"id": "MiniMax-Text-01",  "label": "MiniMax Text-01"},
        ],
    },
    "glm": {
        "name": "GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": [
            {"id": "glm-4-plus",   "label": "GLM-4-Plus"},
            {"id": "glm-4-flash",  "label": "GLM-4-Flash · Free"},
            {"id": "glm-z1-plus",  "label": "GLM-Z1-Plus · Reasoning"},
        ],
    },
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(config: dict):
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


OLLAMA_OPTIONS_DEFAULTS = {
    "num_ctx":        8192,   # context window (token count)
    "temperature":    0.8,    # randomness (higher = more creative)
    "top_p":          0.9,    # nucleus sampling
    "repeat_penalty": 1.1,    # repetition penalty
    "num_predict":    -1,     # max output tokens (-1 = unlimited)
}


def get_ollama_options() -> dict:
    return {**OLLAMA_OPTIONS_DEFAULTS,
            **load_config().get("ollama_options", {})}


def set_ollama_options(options: dict):
    config = load_config()
    # Only save valid keys with basic type validation
    validated = {}
    for k, default in OLLAMA_OPTIONS_DEFAULTS.items():
        if k in options:
            try:
                validated[k] = type(default)(options[k])
            except (ValueError, TypeError):
                validated[k] = default
    config["ollama_options"] = validated
    save_config(config)


def get_cloud_keys() -> dict:
    """Returns {provider: {api_key, enabled}}"""
    return load_config().get("cloud_keys", {})


def set_cloud_keys(keys: dict):
    config = load_config()
    config["cloud_keys"] = keys
    save_config(config)


REMOTE_SERVER_DEFAULTS = {
    "host":        "",
    "user":        "",
    "ssh_port":    22,
    "auth_mode":   "key",          # "key" | "password"
    "key_path":    "~/.ssh/id_rsa",
    "remote_port": 11434,
    "local_port":  11435,
}


def get_remote_config() -> dict:
    return {**REMOTE_SERVER_DEFAULTS,
            **load_config().get("remote_server", {})}


def set_remote_config(cfg: dict):
    config = load_config()
    validated = {}
    for k, default in REMOTE_SERVER_DEFAULTS.items():
        if k in cfg:
            try:
                validated[k] = type(default)(cfg[k])
            except (ValueError, TypeError):
                validated[k] = default
    config["remote_server"] = validated
    save_config(config)


def get_enabled_cloud_models() -> list[dict]:
    """Returns list of cloud models with configured API keys, same format as Ollama models"""
    keys = get_cloud_keys()
    models = []
    for provider, meta in CLOUD_PROVIDERS.items():
        entry = keys.get(provider, {})
        if entry.get("api_key") and entry.get("enabled", True):
            for m in meta["models"]:
                models.append({
                    "id": f"cloud:{provider}:{m['id']}",
                    "label": f"☁️ {m['label']}",
                    "provider": provider,
                })
    return models
