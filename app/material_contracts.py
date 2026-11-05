from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MaterialContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MaterialHit(MaterialContract):
    doc_id: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    title: str = Field(min_length=1, max_length=140)
    excerpt: str = Field(min_length=1, max_length=1600)


class SearchInput(MaterialContract):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=5)


class SearchOutput(MaterialContract):
    status: Literal["ok", "not_found"]
    source: Literal["local_materials"] = "local_materials"
    hits: list[MaterialHit] = Field(max_length=5)


class CatalogItem(MaterialContract):
    doc_id: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    title: str = Field(min_length=1, max_length=140)


class Catalog(MaterialContract):
    documents: list[CatalogItem] = Field(max_length=23)
