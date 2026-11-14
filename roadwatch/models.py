from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


class LocationInput(BaseModel):
    """A street chosen on the map. `geometry` is the OSM way as GeoJSON (LineString/MultiLineString)."""
    road: str = Field(min_length=1, max_length=240)
    display_name: str = Field(default="", max_length=400)
    city_key: str = Field(min_length=1, max_length=40)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    osm_type: str | None = Field(default=None, max_length=20)
    osm_id: int | None = None
    geometry: dict | None = None
    from_street: str = Field(default="", max_length=120)
    to_street: str = Field(default="", max_length=120)

    @field_validator("geometry")
    @classmethod
    def small_geometry(cls, value):
        if value is not None and len(str(value)) > 200_000:
            raise ValueError("Geometry is too large")
        return value


class ProjectInput(BaseModel):
    """A case is anchored on streets in one city (`anchor` = street) or on one supplier followed across governments
    (`anchor` = vendor: `contractor` is the company, `vendor_aliases` its other names, `province` adds that tier)."""
    name: str = Field(min_length=1, max_length=160)
    road: str = Field(default="", max_length=240)
    contractor: str = Field(default="", max_length=160)
    contract_number: str = Field(default="", max_length=100)
    completed_on: date | None = None
    locations: list[LocationInput] = Field(default_factory=list, max_length=12)
    show_condition: bool = True
    anchor: Literal["street", "vendor"] = "street"
    vendor_aliases: list[str] = Field(default_factory=list, max_length=8)
    province: Literal["", "on", "qc"] = ""

    @field_validator("name", "road", "contractor")
    @classmethod
    def trimmed(cls, value):
        return value.strip()

    @field_validator("vendor_aliases")
    @classmethod
    def short_aliases(cls, value):
        if any(len(alias) > 160 for alias in value):
            raise ValueError("Each name may be at most 160 characters")
        return [alias.strip() for alias in value if alias.strip()]

    @model_validator(mode="after")
    def anchor_fields(self):
        if not self.name:
            raise ValueError("Case name cannot be blank")
        if self.anchor == "vendor":
            if not self.contractor:
                raise ValueError("Enter the supplier's name")
            self.road = self.road or "Supplier followed across governments"
        elif not self.road:
            raise ValueError("Road segment cannot be blank")
        return self


class VendorInput(BaseModel):
    """The company a supplier case follows: its name and other names it trades under, and the province whose records to add."""
    name: str = Field(min_length=1, max_length=160)
    aliases: list[str] = Field(default_factory=list, max_length=8)
    province: Literal["", "on", "qc"] = ""

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Enter the supplier's name")
        return value.strip()

    @field_validator("aliases")
    @classmethod
    def short_aliases(cls, value):
        if any(len(alias) > 160 for alias in value):
            raise ValueError("Each name may be at most 160 characters")
        return [alias.strip() for alias in value if alias.strip()]


class EvidenceInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["contract", "amendment", "payment", "inspection", "bid", "open_data", "other"]
    source_url: HttpUrl | None = None
    text: str = Field(default="", max_length=200_000)
    filename: str = Field(default="", max_length=200)
    file_base64: str = Field(default="", max_length=7_000_000)


class ImportInput(BaseModel):
    url: HttpUrl
    title: str = Field(default="", max_length=200)
    kind: Literal["contract", "amendment", "payment", "inspection", "bid", "open_data", "other"] = "other"


class LoginInput(BaseModel):
    """Either an access key (admin bootstrap / legacy member key) or email + password."""
    access_key: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=200)


class InviteAccept(BaseModel):
    code: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class PasswordChange(BaseModel):
    current_password: str = Field(default="", max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


class ProfileUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class SignupInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=1, max_length=200)
    accept_rules: bool = False

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Name cannot be blank")
        return value.strip()


class ForgotPassword(BaseModel):
    email: str = Field(min_length=3, max_length=200)


class FinancialInput(BaseModel):
    kind: Literal["award", "amendment", "payment"]
    amount: Decimal = Field(max_digits=15, decimal_places=2)
    basis: Literal["before_tax", "including_hst", "net_hst_recoveries"]
    scope: str = Field(min_length=1, max_length=160)
    reference: str = Field(min_length=1, max_length=160)
    evidence_id: str
    page: int = Field(ge=1)
    quote: str = Field(min_length=4, max_length=2000)

    @field_validator("scope", "reference", "quote")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("This field cannot be blank")
        return value.strip()

    @field_validator("amount")
    @classmethod
    def finite(cls, value):
        if not value.is_finite():
            raise ValueError("Amount must be finite")
        return value


class ObservationInput(BaseModel):
    observed_on: date
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    severity: Literal["minor", "moderate", "severe"]
    description: str = Field(min_length=10, max_length=4000)


class InvestigationInput(BaseModel):
    use_ai: bool = False
    depth: Literal["quick", "deep"] = "quick"
    provider: str | None = Field(default=None, max_length=60, pattern=r"^[a-z0-9_]+$")


class LimitsInput(BaseModel):
    from_street: str = Field(default="", max_length=120)
    to_street: str = Field(default="", max_length=120)


class SettingsInput(BaseModel):
    show_condition: bool


class OpenDataInput(BaseModel):
    since_year: int = Field(default=2015, ge=2000, le=2100)


class MemberInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: Literal["viewer", "investigator", "counsel", "admin"] = "investigator"

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Name cannot be blank")
        return value.strip()


class MemberUpdate(BaseModel):
    role: Literal["viewer", "investigator", "counsel", "admin"] | None = None
    active: bool | None = None
    reset_key: bool = False
    reset_password: bool = False


class Assessment(BaseModel):
    """A structured conclusion: what the evidence supports, under which rule, how serious, what to do next."""
    summary: str = Field(min_length=10, max_length=4000)
    legal_basis: str = Field(default="", max_length=2000)
    risk: Literal["low", "medium", "high", "unclear"] = "unclear"
    recommended_action: str = Field(default="", max_length=2000)
    status: Literal["draft", "reviewed"] = "draft"


class CommentInput(BaseModel):
    kind: Literal["comment", "assessment"] = "comment"
    anchor_type: Literal["case", "finding", "evidence", "observation"] = "case"
    anchor_id: str = Field(default="", max_length=120)
    anchor_label: str = Field(default="", max_length=240)
    parent_id: str | None = Field(default=None, max_length=60)
    body: str = Field(default="", max_length=6000)
    citations: list["Citation"] = Field(default_factory=list, max_length=8)
    assessment: Assessment | None = None


class CommentUpdate(BaseModel):
    body: str | None = Field(default=None, max_length=6000)
    assessment: Assessment | None = None


class FindingStatusInput(BaseModel):
    status: Literal["open", "investigating", "explained", "escalated", "dismissed"]
    assignee_id: str | None = Field(default=None, max_length=60)
    note: str = Field(default="", max_length=2000)
    title: str = Field(default="", max_length=200)


class Citation(BaseModel):
    evidence_id: str
    page: int = Field(ge=1)
    quote: str = Field(min_length=4, max_length=2000)


class AIFinding(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=1, max_length=4000)
    alternative_explanation: str = Field(min_length=1, max_length=2000)
    next_step: str = Field(min_length=1, max_length=2000)
    citations: list[Citation] = Field(min_length=1, max_length=8)
    confidence: Literal["supported", "plausible", "speculative"] = "plausible"
    tests_performed: list[str] = Field(default_factory=list, max_length=8)
    what_would_change: str = Field(default="", max_length=1000)


class Answer(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=2000)
    citations: list[Citation] = Field(default_factory=list, max_length=4)


class AIResult(BaseModel):
    findings: list[AIFinding] = Field(max_length=12)
    missing_records: list[str] = Field(max_length=15)
    answers: list[Answer] = Field(default_factory=list, max_length=10)
