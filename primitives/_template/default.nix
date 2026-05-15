{ pkgs ? import <nixpkgs> {} }:

# Shared nix derivation for every closure. The closure is a normal
# Python project; hatchling reads `pyproject.toml` for metadata and the
# wheel package list. The runtime discovers the closure via
# `importlib.metadata.entry_points` at start-up — no manifest.json file
# needed, no postInstall metadata generation.
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

  meta.description = "Agentix closure (${pyproject.project.name})";
}
