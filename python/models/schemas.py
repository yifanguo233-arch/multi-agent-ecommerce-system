from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class UserSegment(str, Enum):
    NEW_USER = "new_user"  # 新用户
    ACTIVE = "active"  # 活跃用户
    HIGH_VALUE = "high_value"  # 高价值用户
    PRICE_SENSITIVE = "price_sensitive"  # 价格敏感用户
    CHURN_RISK = "churn_risk"  # 流失风险用户


class UserProfile(BaseModel):
    user_id: str  
    age: int | None = None  # 用户年龄，未知时为空
    gender: str | None = None  # 用户性别
    city: str | None = None  # 用户所在城市
    segments: list[UserSegment] = Field(default_factory=list)  # 用户分群标签
    preferred_categories: list[str] = Field(default_factory=list)  # 用户偏好的商品类目
    price_range: tuple[float, float] = (0.0, 10000.0)  # 用户可接受的价格区间
    recent_views: list[str] = Field(default_factory=list)  # 最近浏览过的商品或类目
    recent_purchases: list[str] = Field(default_factory=list)  # 最近购买过的商品或类目
    rfm_score: dict[str, float] = Field(default_factory=dict)  # RFM 分数，包含近度、频次和金额
    real_time_tags: dict[str, Any] = Field(default_factory=dict)  # 实时画像标签


class ShortTermMemory(BaseModel):
    session_id: str = ""  # 当前会话 ID
    recent_views_1h: list[str] = Field(default_factory=list)  # 最近 1 小时浏览行为
    recent_clicks_1h: list[str] = Field(default_factory=list)  # 最近 1 小时点击行为
    recent_purchases_24h: list[str] = Field(default_factory=list)  # 最近 24 小时购买行为
    intent_categories: list[str] = Field(default_factory=list)  # 根据近期行为推断出的意图类目
    active_minutes_30m: int = 0  # 最近 30 分钟估算活跃时长


class LongTermMemory(BaseModel):
    preferred_categories_30d: list[str] = Field(default_factory=list)  # 近 30 天偏好类目
    preferred_brands_30d: list[str] = Field(default_factory=list)  # 近 30 天偏好品牌
    price_sensitivity: float = 0.5  # 价格敏感度，越高越偏向低价或促销
    avg_order_amount_30d: float = 0.0  # 近 30 天平均订单金额
    purchase_power_tier: str = "unknown"  # 用户购买力等级
    churn_risk_score: float = 0.0  # 流失风险分数


class UserContextSnapshot(BaseModel):
    user_id: str  
    scene: str = "homepage"  # 推荐场景；分类逻辑见 services/auto_planner.py:91、111、127
    short_term: ShortTermMemory = Field(default_factory=ShortTermMemory)  
    long_term: LongTermMemory = Field(default_factory=LongTermMemory)  
    intent: dict[str, Any] = Field(default_factory=dict)  # 当前请求下推断出的用户意图
    preference: dict[str, Any] = Field(default_factory=dict)  # 汇总后的用户偏好信息
    risk_flags: list[str] = Field(default_factory=list)  # 风险标签，例如高价格敏感或高流失风险
    freshness_seconds: int = 0  # 用户上下文的新鲜度，单位秒
    generated_at: datetime = Field(default_factory=datetime.now)  # 上下文生成时间


class ExecutionPlan(BaseModel):
    plan_version: str = "v1"  
    retrieve_strategy: str = "hybrid"  # 商品召回策略；白名单见 services/auto_planner.py:237，分支消费见 agents/product_rec_agent.py:174、176、180、182
    rerank_focus: str = "balanced"  # 商品重排重点；白名单见 services/auto_planner.py:238，本地分支见 agents/product_rec_agent.py:416、421、423、435
    copy_tone: str = "default"  # 营销文案语气；白名单见 services/auto_planner.py:239，文案分支见 agents/marketing_copy_agent.py:160、169、171、173、175
    risk_policy: str = "standard"  # 风险控制策略；白名单见 services/auto_planner.py:240，retention_boost 分支见 agents/inventory_agent.py:136、141 和 orchestrator/graph.py:219、226；stock_guard 目前主要透传
    business_goal: str = "conversion"  # 业务目标；当前主要透传，Planner 写入见 services/auto_planner.py:91、136，暂无独立分支
    filters: dict[str, Any] = Field(default_factory=dict)  # 计划附带的过滤或排序约束
    metadata: dict[str, Any] = Field(default_factory=dict)  # 计划生成过程中的补充信息

    @classmethod
    def default(cls) -> "ExecutionPlan":
        return cls(
            plan_version="v1",
            retrieve_strategy="hybrid",
            rerank_focus="balanced",
            copy_tone="default",
            risk_policy="standard",
            business_goal="conversion",
        )


class Product(BaseModel):
    product_id: str 
    name: str  
    category: str  
    price: float  
    description: str = "" 
    brand: str = "" 
    seller_id: str = ""  # 商家或卖家 ID
    stock: int = 0  
    tags: list[str] = Field(default_factory=list) 
    score: float = 0.0  # 商品基础分或热度分
    image_url: str = ""  # 商品图片地址


class RecommendationRequest(BaseModel):
    user_id: str  
    scene: str = "homepage"  # 推荐场景；分类逻辑见 services/auto_planner.py:91、111、127
    num_items: int = 10  
    business_goal: str = "conversion"  # 本次推荐的业务目标；当前主要透传，Planner 写入见 services/auto_planner.py:91、136
    context: dict[str, Any] = Field(default_factory=dict)  # 请求上下文；trigger_event 分类见 services/auto_planner.py:103、111，召回 query 拼接见 agents/product_rec_agent.py:255、270


class BehaviorEventRequest(BaseModel):
    user_id: str  
    behavior_type: Literal["view", "click", "purchase"]  # 行为类型
    item_id: str  # 行为关联的商品或对象 ID
    metadata: dict[str, Any] = Field(default_factory=dict)  # 行为附加信息，例如类目、品牌或金额


class OfflineTagsRequest(BaseModel):
    tags: dict[str, Any] = Field(default_factory=dict)  # 离线计算得到的用户标签


class AgentResult(BaseModel):
    agent_name: str 
    success: bool = True  # Agent 是否执行成功
    latency_ms: float = 0.0  # Agent 执行耗时，单位毫秒
    error: str | None = None  # Agent 失败时的错误信息
    data: dict[str, Any] = Field(default_factory=dict)  # Agent 返回的调试或补充数据
    confidence: float = 1.0  # Agent 输出结果的置信度


class UserProfileResult(AgentResult):
    agent_name: str = "user_profile"  
    profile: UserProfile | None = None 


class ProductRecResult(AgentResult):
    agent_name: str = "product_rec" 
    products: list[Product] = Field(default_factory=list)  # 召回或重排后的商品列表
    recall_strategy: str = ""  # 本次使用的召回策略说明


class MarketingCopyResult(AgentResult):
    agent_name: str = "marketing_copy"  
    copies: list[dict[str, str]] = Field(default_factory=list)  # 每个商品对应的营销文案
    prompt_template_used: str = ""  # 使用的提示词模板名称


class InventoryResult(AgentResult):
    agent_name: str = "inventory"  
    available_products: list[str] = Field(default_factory=list)  # 有库存的商品 ID 列表
    low_stock_alerts: list[dict[str, Any]] = Field(default_factory=list)  # 低库存告警信息
    purchase_limits: dict[str, int] = Field(default_factory=dict)  # 商品限购数量


class PlannerResult(AgentResult):
    agent_name: str = "planner"  
    plan_hit: bool = False  # 是否成功生成或命中执行计划
    execution_plan: ExecutionPlan = Field(default_factory=ExecutionPlan)  # 推荐链路执行计划


class RecommendationResponse(BaseModel):
    request_id: str  
    user_id: str 
    products: list[Product] = Field(default_factory=list)  #
    marketing_copies: list[dict[str, str]] = Field(default_factory=list) 
    experiment_group: str = "control" 
    context_version: str = "v1"  
    memory_hit: bool = False  # 是否成功构建或命中用户上下文
    plan_version: str = "v1"  
    plan_hit: bool = False  # 是否成功生成或命中执行计划
    execution_plan: dict[str, Any] = Field(default_factory=dict)  
    plan_payload: dict[str, Any] = Field(default_factory=dict)  # LangGraph workflow 内部使用的计划载荷
    plan_replay_key: str = ""  # 用于定位计划或链路回放的 key
    planner_observability: dict[str, Any] = Field(default_factory=dict)  # Planner 可观测信息
    debug_context: dict[str, Any] = Field(default_factory=dict)  # 调试上下文信息
    agent_results: dict[str, AgentResult] = Field(default_factory=dict)  
    total_latency_ms: float = 0.0  # 推荐链路总耗时，单位毫秒
    timestamp: datetime = Field(default_factory=datetime.now)  # 响应生成时间
