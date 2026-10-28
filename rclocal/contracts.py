"""The contract registry: what every file in the project promises to the others.

One JSON file per project file in `.hub/contracts/`. A contract names the file's
purpose, its exports (name, signature, description) and the project files it imports.
Agents read contracts, never each other's code: a builder sees its own file's contract
plus the contracts of the files it imports, and a tester sees only the contract.

Anyone can propose a change, but only this class writes, under the hub's lock. Every
change bumps the version and is kept in the contract's history, and `CONTRACTS.md`
is rendered from the registry for people to read.
"""

from __future__ import annotations

from .protocol import FILE_PLAN, validate


class ContractError(ValueError):
    """A contract the registry refuses."""


def file_id(path):
    """The task id for a file: app/geo.py -> app-geo-py."""
    return path.replace("/", "-").replace("_", "-").replace(".", "-")


class Contracts:
    def __init__(self, hub):
        self.hub = hub
        self.dir = hub.root / "contracts"

    @property
    def stack(self):
        return self.hub.stack

    def check_path(self, path):
        if not self.stack.check_path(path):
            raise ContractError(f"{path!r} must be a lowercase path in app/, like {self.stack.example}")
        return path

    def _path(self, path):
        return self.dir / f"{file_id(path)}.json"

    def all(self):
        return sorted((self.hub._read(p) for p in self.dir.glob("*.json")), key=lambda c: c["path"])

    def get(self, path):
        p = self._path(path)
        if not p.exists():
            raise ContractError(f"no contract for {path}")
        return self.hub._read(p)

    def exists(self, path):
        return self._path(path).exists()

    def by_task(self, task_id):
        for contract in self.all():
            if task_id in (contract["id"], contract["id"] + "-tests"):
                return contract
        raise ContractError(f"no contract for task {task_id}")

    def upsert(self, plan, round_no, reason):
        """Add a file's contract, or change it. Returns (contract, what) with what in new/changed/same."""
        validate(plan, FILE_PLAN, "file plan")
        path = self.check_path(plan["path"])
        fields = {
            "purpose": plan["purpose"].strip(),
            "exports": sorted(plan["exports"], key=lambda e: e["name"]),
            "imports": sorted(set(plan["imports"]) - {path}),
        }
        with self.hub.locked():
            if not self.exists(path):
                contract = {
                    "id": file_id(path),
                    "path": path,
                    "module": self.stack.import_name(path),
                    "test_path": self.stack.test_path(path),
                    "version": 1,
                    "history": [{"version": 1, "round": round_no, "reason": reason}],
                    **fields,
                }
                self.hub._write(self._path(path), contract)
                return contract, "new"
            contract = self.get(path)
            if all(contract[k] == v for k, v in fields.items()):
                return contract, "same"
            contract.update(fields)
            self._bump(contract, round_no, reason)
            return contract, "changed"

    def add_exports(self, path, exports, round_no, reason):
        """Add exports another file asked for. Names already exported are left alone."""
        with self.hub.locked():
            contract = self.get(path)
            have = {e["name"] for e in contract["exports"]}
            added = [e for e in exports if e["name"] not in have]
            if not added:
                return contract, []
            contract["exports"] = sorted(contract["exports"] + added, key=lambda e: e["name"])
            self._bump(contract, round_no, reason)
            return contract, [e["name"] for e in added]

    def remove(self, path):
        """Forget a contract (a piece of a split that didn't come together)."""
        with self.hub.locked():
            self._path(path).unlink(missing_ok=True)

    def mark(self, path, **fields):
        """Keep facts about a file next to its contract (like which file it was split from). No new version."""
        with self.hub.locked():
            contract = self.get(path)
            contract.update(fields)
            self.hub._write(self._path(path), contract)
            return contract

    def set_imports(self, path, imports, round_no, reason):
        with self.hub.locked():
            contract = self.get(path)
            contract["imports"] = sorted(set(imports))
            self._bump(contract, round_no, reason)
            return contract

    def _bump(self, contract, round_no, reason):
        contract["version"] += 1
        contract["history"].append({"version": contract["version"], "round": round_no, "reason": reason})
        self.hub._write(self._path(contract["path"]), contract)

    def dependents(self, path):
        """Every file that imports this one, directly or through another file."""
        contracts = self.all()
        found, stack = set(), [path]
        while stack:
            target = stack.pop()
            for c in contracts:
                if target in c["imports"] and c["path"] not in found:
                    found.add(c["path"])
                    stack.append(c["path"])
        return sorted(found)

    def check(self):
        """Problems in the registry: imports of files that have no contract."""
        known = {c["path"] for c in self.all()}
        return [
            f"{c['path']} imports {imp}, which has no contract"
            for c in self.all()
            for imp in c["imports"]
            if imp not in known
        ]

    # What agents and people read

    def describe(self, contract):
        lines = [
            f"File {contract['path']} ({self.stack.import_hint(contract['path'])}), contract v{contract['version']}",
            f"Purpose: {contract['purpose']}",
            "Exports:",
        ]
        for e in contract["exports"]:
            lines.append(f"  {e['signature']}  # {e['description']}")
            lines += [f"      e.g. {x['call']} returns {x['returns']}" for x in e.get("examples", [])]
        if not contract["exports"]:
            lines.append("  (nothing)")
        return "\n".join(lines)

    def brief(self, path):
        """A file's own contract, then the contracts of the files it imports."""
        contract = self.get(path)
        parts = [self.describe(contract)]
        for imp in contract["imports"]:
            if self.exists(imp):
                parts.append("It may use:\n" + self.describe(self.get(imp)))
        return "\n\n".join(parts)

    def render(self):
        out = [
            "# Contracts",
            "",
            "_What each file promises to the others. Generated from `.hub/contracts/`, so don't edit it by hand._",
            "",
        ]
        for c in self.all():
            out += [f"## `{c['path']}` (v{c['version']})", "", c["purpose"], ""]
            for e in c["exports"]:
                out.append(f"- `{e['signature']}`: {e['description']}")
                out += [f"  - `{x['call']}` returns `{x['returns']}`" for x in e.get("examples", [])]
            if c["imports"]:
                out += ["", "Uses: " + ", ".join(f"`{i}`" for i in c["imports"])]
            out += [f"", f"Tests: `{c['test_path']}`", ""]
        path = self.hub.project / "CONTRACTS.md"
        self.hub._write_text(path, "\n".join(out))
        return path
