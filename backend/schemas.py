# backend/schemas.py
from typing import Annotated, List, Optional, Dict
from pydantic import BaseModel, Field

# ---- Reusable constrained int (1..100) ----
Int_1_to_100 = Annotated[int, Field(ge=1, le=100, description="1–100 inclusive")]


# ---- Request ----
class GenerateKeywordsRequest(BaseModel):
    topic: str = Field(..., min_length=2, description="Main topic, e.g., 'running shoes'")
    brand: Optional[str] = Field(None, description="Brand name, e.g., 'Nike'")
    modifiers: Optional[str] = Field(None, description="Optional qualifiers, e.g., 'for flat feet, waterproof'")
    num_keywords: Int_1_to_100 = 10
    model: str = Field("gpt-5", description="OpenAI model id to use")  # default updated to gpt-5


# ---- Response Rows ----
class KeywordRow(BaseModel):
    keyword: str
    competition: str = "NA"
    searchVolume: str = "NA"
    cpc: str = "NA"


class Usage(BaseModel):
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


# ---- Responses ----
class GenerateKeywordsResponse(BaseModel):
    source: str
    model: str
    usage: Usage
    rows: List[KeywordRow]

    # Add quota fields (for alignment with backend)
    usdSpent: Optional[float] = None 
    quotaLeft: Optional[int] = None
    quota: Optional[Dict] = None
    openaiQuota: Optional[Dict] = None
    

# ---- Async Task ----
class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    result: Optional[dict] = None
