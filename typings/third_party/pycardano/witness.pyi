from dataclasses import dataclass

from .key import ExtendedVerificationKey, VerificationKey

@dataclass
class VerificationKeyWitness:
    vkey: VerificationKey | ExtendedVerificationKey
    signature: bytes
    def __init__(
        self, vkey: VerificationKey | ExtendedVerificationKey, signature: bytes
    ) -> None: ...
