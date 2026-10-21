from pydantic import BaseModel, Field


class ContactPolicyRule(BaseModel):
    agents: str
    budget: int


class UserRegisterRequest(BaseModel):
    uid: str
    password: str
    verify_key_hex: str


class AgentRegisterRequest(BaseModel):
    aid: str
    uid: str
    password: str
    device: str
    ip: str
    port: int
    pac_hex: str
    otks: list[str]
    signature_hex: str
    otk_signatures: list[str]
    contact_policy: list[ContactPolicyRule] = Field(default_factory=list)


class OTKRefreshRequest(BaseModel):
    uid: str
    password: str
    otks: list[str]
    otk_signatures: list[str]


class PolicyUpdateRequest(BaseModel):
    uid: str
    password: str
    contact_policy: list[ContactPolicyRule]


class DeactivateRequest(BaseModel):
    uid: str
    password: str


class LookupResponse(BaseModel):
    aid: str
    device: str
    ip: str
    port: int
    pac_hex: str
    otk_hex: str
    user_verify_key_hex: str
    agent_signature_hex: str
    provider_signature_hex: str
