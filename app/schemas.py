"""Pydantic request schemas for input validation.

v3: all mutation endpoints now validate input via Pydantic models instead
of ad-hoc int()/float() calls that could throw 500 errors on bad input.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    current: str = Field(..., min_length=1, max_length=256)
    new: str = Field(..., min_length=6, max_length=256)


class CreateUserRequest(BaseModel):
    uuid: Optional[str] = None
    label: str = Field(default="کاربر", max_length=48)
    days: int = Field(default=0, ge=0, le=3650)
    gb: float = Field(default=0, ge=0, le=100000)
    protocols: list[Literal["ws", "grpc", "reality"]] = Field(
        default_factory=lambda: ["ws", "grpc", "reality"],
        min_length=1,  # at least one protocol required
    )
    ws_ips: str = Field(default="", max_length=1024)
    reality_sni: str = Field(default="", max_length=256)


class EditUserRequest(BaseModel):
    label: Optional[str] = Field(default=None, max_length=48)
    add_days: Optional[int] = Field(default=None, ge=0, le=3650)
    gb: Optional[float] = Field(default=None, ge=0, le=100000)
    protocols: Optional[list[Literal["ws", "grpc", "reality"]]] = Field(
        default=None, min_length=1
    )
    ws_ips: Optional[str] = Field(default=None, max_length=1024)
    reality_sni: Optional[str] = Field(default=None, max_length=256)
    status: Optional[Literal["active", "disabled"]] = None
