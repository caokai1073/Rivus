"""
config.py — 应用配置持久化（API 密钥等）
"""
APP_VERSION = "1.0.0"
# 填入 GitHub releases API URL 后即可启用自动更新检测
# 例: "https://api.github.com/repos/yourname/rivus/releases/latest"
UPDATE_CHECK_URL = ""

import json
import os
from pathlib import Path

_data_dir = os.environ.get("MEMORYVAULT_DATA_DIR")
CONFIG_PATH = (
    Path(_data_dir) / "config.json" if _data_dir
    else Path(__file__).parent / "config.json"
)

# 云端提供商元数据
CLOUD_PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": [
            {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash"},
            {"id": "deepseek-v4-pro",   "label": "DeepSeek V4 Pro"},
        ],
    },
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
    "minimax": {
        "name": "MiniMax",
        "base_url": "https://api.minimax.chat/v1",
        "models": [
            {"id": "MiniMax-M1",       "label": "MiniMax M1 · Flagship"},
            {"id": "MiniMax-Text-01",  "label": "MiniMax Text-01"},
        ],
    },
    "glm": {
        "name": "智谱 GLM",
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
    "num_ctx":        4096,   # 上下文窗口（token 数）
    "temperature":    0.8,    # 随机性（越高越有创意）
    "top_p":          0.9,    # nucleus sampling
    "repeat_penalty": 1.1,    # 重复惩罚
    "num_predict":    -1,     # 最大输出 token（-1 = 不限）
}


def get_ollama_options() -> dict:
    return {**OLLAMA_OPTIONS_DEFAULTS,
            **load_config().get("ollama_options", {})}


def set_ollama_options(options: dict):
    config = load_config()
    # 只保存有效键，值做基本类型校验
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
    """返回 {provider: {api_key, enabled}} """
    return load_config().get("cloud_keys", {})


def set_cloud_keys(keys: dict):
    config = load_config()
    config["cloud_keys"] = keys
    save_config(config)


def get_enabled_cloud_models() -> list[dict]:
    """返回已配置 API Key 的云端模型列表，格式同 Ollama models"""
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
