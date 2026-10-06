# Model Providers & Reasoning Router

> 22+ supported LLM providers, Jev dynamic model routing (<70ms), and unified reasoning effort

OpsCloud is engineered for multi-model flexibility. It supports 22+ LLM providers, features the ultra-low latency **TypeSafe AI Jev Dynamic Model Router (<70ms)**, provides unified reasoning effort controls across models, and runs fully offline with Ollama.

---

## Model Format

Models are specified using the canonical `provider:model-name` format:

```text
anthropic:claude-3-7-sonnet-20250219
openai:gpt-4o
google_genai:gemini-2.5-pro
deepseek:deepseek-reasoner
bedrock:anthropic.claude-3-5-sonnet-20241022-v2:0
ollama:llama3.3
```

Set the model via:
- **CLI flag:** `opscloud -M anthropic:claude-3-7-sonnet-20250219`
- **Persistent default:** `opscloud --default-model openai:gpt-4o`
- **Interactive session:** Type `/model` to open the picker, or `/model <specifier>`
- **Configuration file:** Set `[model].default` in `~/.opscloud/config.toml`
- **Subagent override:** Specify `model: provider:model-name` in the subagent's frontmatter

---

## Supported Providers

| Provider | Identifier | Required Auth | Highlights |
|---|---|---|---|
| **Anthropic** | `anthropic` | `ANTHROPIC_API_KEY` | Claude 3.5 Sonnet, Claude 3.7 Sonnet Thinking, Vision |
| **OpenAI** | `openai` | `OPENAI_API_KEY` | GPT-4o, GPT-4o-mini, o1, o3-mini Reasoning |
| **TypeSafe AI (Jev)** | `typesafe` / `dynamic` | `TYPESAFE_API_KEY` | Dynamic model router (<70ms), System One safety gate (<100ms) |
| **Google GenAI** | `google_genai` | `GOOGLE_API_KEY` | Gemini 2.0 / 2.5 Flash & Pro, Flash Thinking |
| **Google Vertex AI** | `google_vertexai`| ADC (`GOOGLE_CLOUD_PROJECT`) | Enterprise Google Cloud endpoints with IAM auth |
| **AWS Bedrock** | `bedrock` | `AWS_PROFILE` / IAM | Anthropic Claude & Amazon Nova via AWS IAM credentials |
| **Azure OpenAI** | `azure_openai` | `AZURE_OPENAI_API_KEY` | Enterprise private OpenAI deployments |
| **Groq** | `groq` | `GROQ_API_KEY` | Ultra-fast inference on Llama 3.3, DeepSeek R1 |
| **DeepSeek** | `deepseek` | `DEEPSEEK_API_KEY` | DeepSeek V3, DeepSeek R1 Reasoning |
| **Together AI** | `together` | `TOGETHER_API_KEY` | Open-source foundation models |
| **Fireworks AI** | `fireworks` | `FIREWORKS_API_KEY` | Ultra-fast function calling endpoints |
| **OpenRouter** | `openrouter` | `OPENROUTER_API_KEY` | Universal LLM routing gateway |
| **Mistral AI** | `mistralai` | `MISTRAL_API_KEY` | Mistral Large, Codestral, Pixtral |
| **NVIDIA NIM** | `nvidia` | `NVIDIA_API_KEY` | GPU-accelerated microservices |
| **Perplexity** | `perplexity` | `PPLX_API_KEY` | Search-augmented Sonar reasoning models |
| **Cohere** | `cohere` | `COHERE_API_KEY` | Command R / R+ enterprise models |
| **IBM watsonx** | `ibm` | `WATSONX_APIKEY` | Enterprise Granite models |
| **HuggingFace** | `huggingface` | `HUGGINGFACEHUB_API_TOKEN` | Dedicated Inference Endpoints |
| **LiteLLM** | `litellm` | `LITELLM_API_KEY` | Unified proxy for enterprise internal gateways |
| **xAI** | `xai` | `XAI_API_KEY` | Grok 2 / Grok 3 |
| **Baseten** | `baseten` | `BASETEN_API_KEY` | Custom deployed container models |
| **Ollama** | `ollama` | Optional | Fully offline local model inference |

---

## Jev TypeSafe Dynamic Model Router (<70ms)

When running in Smart mode, OpsCloud utilizes the **Jev TypeSafe Dynamic Model Router**. It classifies incoming prompts and operational context in under 70ms and routes them to the optimal model tier:

- **Fast Tier** (e.g. Claude Haiku, GPT-4o-mini, Gemini Flash): Read-only inspections, syntax linting, git status checks, and simple file edits.
- **Standard Tier** (e.g. Claude Sonnet, GPT-4o, Gemini Pro): General DevOps coding, Kubernetes manifests, and Terraform module creation.
- **Powerful Tier** (e.g. Claude 3.7 Thinking, OpenAI o1/o3, DeepSeek R1): Multi-file architectural refactoring, distributed system changes, and deep incident root-cause analysis.

### Manage Router Pools via CLI

```bash
# View active pool tiers and assigned models
opscloud pool show

# Override a specific tier
opscloud pool set fast anthropic:claude-3-5-haiku-latest
opscloud pool set standard anthropic:claude-3-5-sonnet-latest
opscloud pool set powerful anthropic:claude-3-7-sonnet-20250219

# Reset to provider defaults
opscloud pool reset
```

### Configure Pools in `config.toml`

```toml
[agent_pool]
provider = "anthropic"
fast = "anthropic:claude-3-5-haiku-latest"
standard = "anthropic:claude-3-5-sonnet-latest"
powerful = "anthropic:claude-3-7-sonnet-20250219"
fast_effort = "off"
standard_effort = "low"
powerful_effort = "high"
```

Inside an interactive session, use `/pool` to switch tiers, or toggle `/fast` for rapid execution.

---

## Unified Reasoning Effort Controls

OpsCloud provides a unified reasoning abstraction across providers that support extended thinking (Claude 3.7 Sonnet Thinking, OpenAI o1/o3-mini, Gemini 2.0 Flash Thinking, DeepSeek R1).

Control reasoning effort interactively:

```text
/effort off       # Standard execution (no extended thinking)
/effort low       # Fast reasoning for straightforward operational tasks
/effort medium    # Balanced thinking for multi-step DevOps workflows
/effort high      # Deep reasoning for distributed architecture and subtle debugging
```

Or set the default in `~/.opscloud/config.toml`:

```toml
[model]
reasoning_effort = "medium"
```
