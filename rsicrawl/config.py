"""Runtime configuration, from environment variables (all optional).

RSICRAWL_LIGHTPANDA      path to the Lightpanda binary (default: `lightpanda` on PATH, else ./lightpanda)
RSICRAWL_LLM_PROVIDER    "anthropic" (Claude API; uses ANTHROPIC_API_KEY or `ant auth login`) or "bedrock"
                         (default: "anthropic" if ANTHROPIC_API_KEY is set, otherwise "bedrock")
RSICRAWL_AWS_REGION      Bedrock region (default: AWS_REGION, else us-west-2)
RSICRAWL_BEDROCK_PREFIX  prefix for Bedrock model ids (default "us.anthropic.")
RSICRAWL_MODEL_AGENT     model for investigate/distill/improve episodes (default claude-opus-5-5)
RSICRAWL_MODEL_SONNET    optional mid-tier model id (default claude-sonnet-5-5)
RSICRAWL_MODEL_REFLECT   model for lesson writing (default claude-haiku-5-5)
RSICRAWL_USER_AGENT      crawler User-Agent (default identifies RSICrawl and links the repository)
RSICRAWL_SANDBOX         "docker" (network-less container; recommended) or "process" (rlimits only; default
                         "docker" when the docker CLI is available, else "process")
RSICRAWL_SANDBOX_IMAGE   image for the docker sandbox (default python:3.13-slim)
"""
import os, shutil

REPO_URL = "https://github.com/model-collapse/RSICrawl"


def lightpanda_bin():
    return os.environ.get("RSICRAWL_LIGHTPANDA") or os.environ.get("LIGHTPANDA_BIN") or shutil.which("lightpanda") or "./lightpanda"


def llm_provider():
    p = os.environ.get("RSICRAWL_LLM_PROVIDER")
    if p: return p
    return "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "bedrock"


def aws_region():
    return os.environ.get("RSICRAWL_AWS_REGION") or os.environ.get("AWS_REGION") or "us-west-2"


def model_ids():
    base = {"opus": os.environ.get("RSICRAWL_MODEL_AGENT", "claude-opus-5-5"),
            "sonnet": os.environ.get("RSICRAWL_MODEL_SONNET", "claude-sonnet-5-5"),
            "haiku": os.environ.get("RSICRAWL_MODEL_REFLECT", "claude-haiku-5-5")}
    if llm_provider() == "bedrock":
        pre = os.environ.get("RSICRAWL_BEDROCK_PREFIX", "us.anthropic.")
        return {k: (v if v.startswith(pre) or "." in v.split("claude")[0] else pre + v) for k, v in base.items()}
    return base


USER_AGENT = os.environ.get("RSICRAWL_USER_AGENT", f"RSICrawl/0.1 (+{REPO_URL})")


def sandbox_mode():
    m = os.environ.get("RSICRAWL_SANDBOX")
    if m: return m
    return "docker" if shutil.which("docker") else "process"


SANDBOX_IMAGE = os.environ.get("RSICRAWL_SANDBOX_IMAGE", "python:3.13-slim")
