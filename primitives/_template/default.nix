{ pkgs ? import <nixpkgs> {} }:

# Shared nix derivation for every namespace. The namespace is a normal
# Python project; hatchling reads `pyproject.toml` for metadata and the
# wheel package list. The runtime discovers the namespace via
# `importlib.metadata.entry_points` at start-up.
let
  python = pkgs.python312;
  pythonPkgs = python.pkgs;
  pyproject = builtins.fromTOML (builtins.readFile ./pyproject.toml);
in
pythonPkgs.buildPythonApplication {
  pname = pyproject.project.name;
  version = pyproject.project.version;
  format = "pyproject";
  src = ./.;

  nativeBuildInputs = [ pythonPkgs.hatchling ];
  propagatedBuildInputs = [];
  doCheck = false;

  meta.description = "Agentix namespace (${pyproject.project.name})";
}
