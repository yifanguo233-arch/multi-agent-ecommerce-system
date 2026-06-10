from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    app_name: str = "Multi-Agent E-Commerce System"
    debug: bool = False

    # LLM 配置
    llm_api_key: str = ""
    llm_base_url: str = "https://api.minimax.chat/v1"
    llm_model: str = "MiniMax-M1"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 2048

    # Redis 配置
    redis_url: str = "redis://localhost:6379/0"
    feature_ttl_seconds: int = 86400

    # Milvus 配置
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_collection: str = "product_embeddings"

    # 数据库配置
    database_url: str = "sqlite:///./ecommerce.db"
    product_auto_seed: bool = True
    product_seed_count: int = 240
    product_catalog_cache_limit: int = 500

    # A/B 测试配置
    ab_test_enabled: bool = True
    ab_test_default_bucket_count: int = 100

    # Agent 超时时间，单位秒
    agent_timeout_user_profile: float = 5.0
    agent_timeout_product_rec: float = 8.0
    agent_timeout_marketing_copy: float = 10.0
    agent_timeout_inventory: float = 5.0
    agent_timeout_planner: float = 2.0

    # RAG / Embedding 配置
    rag_enabled: bool = True
    rag_backend: str = "auto"  # 可选：auto、faiss、numpy、lexical
    rag_top_k: int = 20
    embedding_model: str = "text-embedding-3-small"

    # Planner 配置
    planner_use_llm: bool = True
    planner_llm_timeout_seconds: float = 1.2
    planner_llm_max_tokens: int = 768

    # Orchestration 配置
    orchestration_mode: str = "graph"  # 可选：graph 或 supervisor；入口分流见 main.py:318、319、360，非 graph 当前走 Supervisor
    checkpoint_backend: str = "sqlite"
    checkpoint_sqlite_path: str = "./ecommerce_checkpoints.db"

    model_config = {"env_file": ".env", "env_prefix": "ECOM_"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()
