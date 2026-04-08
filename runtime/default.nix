{ pkgs ? import <nixpkgs> {} }:

# Nix packaging for hnix-runtime.
# Development uses uv (pyproject.toml + uv.lock).
# Nix build uses buildPythonApplication (reads the same pyproject.toml).

let
  python = pkgs.python312;
  pythonPkgs = python.pkgs;
in
pythonPkgs.buildPythonApplication {
  pname = "hnix-runtime";
  version = "0.1.0";
  format = "pyproject";

  src = ./.;

  nativeBuildInputs = [ pythonPkgs.hatchling ];

  propagatedBuildInputs = [
    pythonPkgs.fastapi
    pythonPkgs.uvicorn
    pythonPkgs.pydantic
    pythonPkgs.python-multipart
    pythonPkgs.httpx
  ];

  doCheck = false;

  meta.description = "harbor-nix runtime server";
}
