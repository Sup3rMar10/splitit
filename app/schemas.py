from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints

from .money import MAX_AMOUNT_MINOR

Text = lambda lo, hi: Annotated[str, StringConstraints(strip_whitespace=True, min_length=lo, max_length=hi)]  # noqa: E731
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
# StrictInt: JSON 12.5, "12" or true are rejected rather than coerced. Money never arrives as a float.
Id = Annotated[StrictInt, Field(ge=1, le=2**53)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterIn(Strict):
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    name: Text(1, 60)
    password: Annotated[str, StringConstraints(min_length=10, max_length=200)]


class LoginIn(Strict):
    email: Annotated[str, StringConstraints(max_length=254)]
    password: Annotated[str, StringConstraints(max_length=200)]


class GroupIn(Strict):
    name: Text(1, 80)
    currency: Currency


class JoinIn(Strict):
    code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=4, max_length=16)]


class SplitIn(Strict):
    user_id: Id
    # equal: ignored. exact: minor units. percentage: basis points (1% = 100). shares: share count.
    value: Annotated[StrictInt, Field(ge=0, le=MAX_AMOUNT_MINOR)] = 1


class ExpenseIn(Strict):
    description: Text(1, 100)
    amount_minor: Annotated[StrictInt, Field(ge=1, le=MAX_AMOUNT_MINOR)]
    currency: Currency
    paid_by: Id
    split_type: Literal["equal", "exact", "percentage", "shares"]
    splits: Annotated[list[SplitIn], Field(min_length=1, max_length=50)]
    expense_date: Optional[str] = Field(default=None, max_length=10)


class SettlementIn(Strict):
    from_user: Id
    to_user: Id
    amount_minor: Annotated[StrictInt, Field(ge=1, le=MAX_AMOUNT_MINOR)]
