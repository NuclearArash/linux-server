import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from cryptography.fernet import Fernet, InvalidToken


REGISTRY_VERSION = 1
BOT_FIELDS = {"id", "name", "repository", "ref", "entrypoint", "enabled"}
BOT_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
GITHUB_PART_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def empty_registry():
    return {"version": REGISTRY_VERSION, "bots": [], "encrypted_credentials": {}}


def validate_bot_definition(definition):
    if not isinstance(definition, dict) or set(definition) != BOT_FIELDS:
        raise ValueError("Bot definition has an invalid shape")

    bot_id = definition["id"]
    if not isinstance(bot_id, str) or not BOT_ID_PATTERN.fullmatch(bot_id):
        raise ValueError("Bot ID must be a 32-character lowercase hexadecimal string")

    name = definition["name"]
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError("Bot name must contain 1 to 80 characters")

    repository = definition["repository"]
    if not isinstance(repository, str):
        raise ValueError("Repository must be a GitHub HTTPS URL")
    parsed_repository = urlsplit(repository)
    if (
        parsed_repository.scheme != "https"
        or parsed_repository.hostname != "github.com"
        or parsed_repository.username is not None
        or parsed_repository.password is not None
        or parsed_repository.port is not None
        or parsed_repository.query
        or parsed_repository.fragment
    ):
        raise ValueError("Repository must be a credential-free GitHub HTTPS URL")
    repository_parts = parsed_repository.path.strip("/").split("/")
    if len(repository_parts) != 2:
        raise ValueError("Repository URL must identify one GitHub repository")
    owner, repository_name = repository_parts
    if repository_name.endswith(".git"):
        repository_name = repository_name[:-4]
    if (
        not GITHUB_PART_PATTERN.fullmatch(owner)
        or not GITHUB_PART_PATTERN.fullmatch(repository_name)
        or owner in {".", ".."}
        or repository_name in {".", ".."}
    ):
        raise ValueError("Repository URL contains an invalid GitHub owner or repository")

    ref = definition["ref"]
    if (
        not isinstance(ref, str)
        or not ref.strip()
        or len(ref) > 255
        or ref.startswith("-")
        or any(ord(character) < 32 or ord(character) == 127 for character in ref)
    ):
        raise ValueError("Git ref is invalid")

    entrypoint = definition["entrypoint"]
    if (
        not isinstance(entrypoint, str)
        or not entrypoint
        or "\\" in entrypoint
        or "\x00" in entrypoint
    ):
        raise ValueError("Entrypoint must be a relative path inside the repository")
    entrypoint_path = PurePosixPath(entrypoint)
    if entrypoint_path.is_absolute() or any(part in {"", ".", ".."} for part in entrypoint.split("/")):
        raise ValueError("Entrypoint must be a normalized relative path inside the repository")

    if not isinstance(definition["enabled"], bool):
        raise ValueError("Bot enabled state must be a boolean")


def validate_registry(registry):
    if not isinstance(registry, dict) or set(registry) != {"version", "bots", "encrypted_credentials"}:
        raise ValueError("Bot registry has an invalid shape")
    if registry["version"] != REGISTRY_VERSION:
        raise ValueError("Bot registry version is unsupported")
    if not isinstance(registry["bots"], list):
        raise ValueError("Bot registry bots field must be a list")

    seen_ids = set()
    for definition in registry["bots"]:
        validate_bot_definition(definition)
        if definition["id"] in seen_ids:
            raise ValueError("Bot registry contains a duplicate ID")
        seen_ids.add(definition["id"])

    credentials = registry["encrypted_credentials"]
    if not isinstance(credentials, dict):
        raise ValueError("Bot registry credentials field must be a mapping")
    for bot_id, encrypted_value in credentials.items():
        if bot_id not in seen_ids or not isinstance(encrypted_value, str) or not encrypted_value:
            raise ValueError("Bot registry contains invalid encrypted credentials")


def _fernet(encryption_key):
    if not isinstance(encryption_key, (str, bytes)) or not encryption_key:
        raise ValueError("Bot credential encryption key is required")
    try:
        return Fernet(encryption_key.encode("ascii") if isinstance(encryption_key, str) else encryption_key)
    except (ValueError, TypeError) as error:
        raise ValueError("Bot credential encryption key is invalid") from error


def set_bot_credentials(registry, bot_id, credentials, encryption_key):
    validate_registry(registry)
    if not any(definition["id"] == bot_id for definition in registry["bots"]):
        raise ValueError("Unknown bot ID")
    if not isinstance(credentials, dict) or set(credentials) != {"pat", "environment"}:
        raise ValueError("Bot credentials have an invalid shape")

    pat = credentials["pat"]
    environment = credentials["environment"]
    if not isinstance(pat, str) or not isinstance(environment, dict):
        raise ValueError("Bot credentials have invalid values")
    for key, value in environment.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError("Bot environment contains an invalid variable name")
        if not isinstance(value, str) or "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError("Bot environment contains an invalid value")

    if not pat and not environment:
        registry["encrypted_credentials"].pop(bot_id, None)
        return

    payload = json.dumps(credentials, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encrypted = _fernet(encryption_key).encrypt(payload).decode("ascii")
    registry["encrypted_credentials"][bot_id] = encrypted


def get_bot_credentials(registry, bot_id, encryption_key):
    validate_registry(registry)
    encrypted_value = registry["encrypted_credentials"].get(bot_id)
    if encrypted_value is None:
        return {"pat": "", "environment": {}}
    try:
        payload = _fernet(encryption_key).decrypt(encrypted_value.encode("ascii"))
        credentials = json.loads(payload.decode("utf-8"))
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("Bot credentials cannot be decrypted") from error
    if not isinstance(credentials, dict) or set(credentials) != {"pat", "environment"}:
        raise ValueError("Decrypted bot credentials have an invalid shape")
    return credentials


def load_registry(path):
    registry_path = Path(path)
    if not registry_path.exists():
        return empty_registry()

    try:
        with registry_path.open("r", encoding="utf-8") as registry_file:
            registry = json.load(registry_file)
        validate_registry(registry)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("Bot registry is unreadable or invalid") from error
    return registry


def save_registry(path, registry):
    validate_registry(registry)
    registry_path = Path(path)
    registry_path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=registry_path.parent,
            prefix=f".{registry_path.name}.",
            delete=False,
        ) as registry_file:
            temporary_path = Path(registry_file.name)
            json.dump(registry, registry_file, indent=2)
            registry_file.write("\n")
            registry_file.flush()
            os.fsync(registry_file.fileno())
        os.replace(temporary_path, registry_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()