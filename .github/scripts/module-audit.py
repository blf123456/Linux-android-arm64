#!/usr/bin/env python3
"""Inspect ELF modules without loading them; fail on known upstream ABI drift.

Requires pyelftools. A passing report is a build comparison, not a device test.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

from elftools.elf.elffile import ELFFile


def describe(path):
    data = path.read_bytes()
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        symtab = elf.get_section_by_name(".symtab")
        if symtab is None:
            raise ValueError("Module has no symbol table")
        symbols = list(symtab.iter_symbols())
        names = {symbol.name for symbol in symbols}
        info = elf.get_section_by_name(".modinfo")
        if info is None:
            raise ValueError("Module has no .modinfo")
        modinfo = dict(item.split("=", 1) for item in
                       info.data().decode().split("\0") if "=" in item)
        module = elf.get_section_by_name(".gnu.linkonce.this_module")
        if module is None:
            raise ValueError("Module has no struct module section")
        comment = elf.get_section_by_name(".comment")
        compilers = sorted(item for item in comment.data().decode().split("\0")
                           if "clang version" in item) if comment else []
        versions = {name: elf.get_section_by_name(name) for name in
                    ("__versions", "__version_ext_crcs", "__version_ext_names")}
        init = next((symbol for symbol in symbols if symbol.name == "init_module"), None)
        # AArch64 KCFI emits the 32-bit type ID immediately before the entry.
        # In these upstream modules init_module starts at offset 4 in .init.text.
        init_type_hash = None
        if init is not None and init["st_value"] == 4 and isinstance(init["st_shndx"], int):
            section = elf.get_section(init["st_shndx"])
            if section.name == ".init.text":
                init_type_hash = section.data()[:4].hex()
        abi = {
            "elf_class": elf.elfclass,
            "little_endian": elf.little_endian,
            "machine": elf["e_machine"],
            "type": elf["e_type"],
            "name": modinfo.get("name"),
            "module_size": module["sh_size"],
            "vermagic_flags": modinfo.get("vermagic", "").split()[1:],
            "version_sections": {name: section.data().hex() if section is not None
                                 else None for name, section in versions.items()},
            "cfi_jump_tables": any(name.endswith(".cfi_jt") for name in names),
            "cfi_check": "__cfi_check" in names,
            "init_type_hash": init_type_hash,
            "compilers": compilers,
        }
        return {
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "vermagic": modinfo.get("vermagic", ""), "abi": abi,
            "init_module": "init_module" in names,
            "undefined_symbols": sorted(symbol.name for symbol in symbols
                                        if symbol["st_shndx"] == "SHN_UNDEF" and symbol.name),
            "sections": {section.name: {"size": section["sh_size"],
                                        "flags": section["sh_flags"]}
                         for section in elf.iter_sections() if section.name},
        }


def compare(actual, expected):
    errors = []
    for key, value in expected["abi"].items():
        if actual["abi"].get(key) != value:
            errors.append(f"{key}: expected {value!r}, got {actual['abi'].get(key)!r}")
    if not actual["init_module"]:
        errors.append("init_module is missing")
    if actual["abi"]["machine"] != "EM_AARCH64" or actual["abi"]["type"] != "ET_REL":
        errors.append("Expected an AArch64 relocatable module")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("module", type=Path)
    parser.add_argument("--kernel", required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reference = json.loads(args.reference.read_text(encoding="utf-8"))
    expected = reference["modules"][args.kernel]
    actual = describe(args.module)
    errors = compare(actual, expected)
    if args.config:
        config = dict(re.findall(r"^(CONFIG_\w+)=(.*)$",
                                args.config.read_text(encoding="utf-8"), re.M))
        for option in ("CONFIG_MODULES", "CONFIG_MODVERSIONS", "CONFIG_ARM64_4K_PAGES"):
            if config.get(option) != "y":
                errors.append(f"Prepared kernel must keep {option}=y")
        actual["kernel_config"] = {key: value for key, value in config.items()
                                   if any(part in key for part in
                                          ("CFI", "LTO", "BTI", "MODVERSION", "RUST", "MODULE_UNLOAD"))}
    report = {
        "kernel": args.kernel, "reference": reference["source"],
        "reference_sha256": expected["sha256"],
        "byte_identical": actual["sha256"] == expected["sha256"],
        "reference_vermagic": expected["vermagic"],
        "errors": errors, "actual": actual,
        "added_imports": sorted(set(actual["undefined_symbols"]) - set(expected["undefined_symbols"])),
        "removed_imports": sorted(set(expected["undefined_symbols"]) - set(actual["undefined_symbols"])),
        "device_loading_tested": False,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for error in errors:
        print(f"ABI mismatch: {error}")
    print(f"{args.kernel}: reference vermagic: {expected['vermagic']}")
    print(f"{args.kernel}: built vermagic:     {actual['vermagic']}")
    if errors:
        raise SystemExit("Module differs from the upstream ABI baseline; refusing to package it")
    print("ABI baseline checks passed; byte equality and device loading are separate checks")


if __name__ == "__main__":
    main()
