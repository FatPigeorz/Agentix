{ pkgs ? import <nixpkgs> {} }:

let
  python = pkgs.python312;
  pythonPkgs = python.pkgs;
in
pythonPkgs.buildPythonApplication {
  pname = "agentix-runtime";
  version = "0.1.0";
  format = "pyproject";

  src = ./..;

  nativeBuildInputs = [ pythonPkgs.hatchling ];

  propagatedBuildInputs = [
    pythonPkgs.fastapi
    pythonPkgs.uvicorn
    pythonPkgs.pydantic
    pythonPkgs.python-multipart
    pythonPkgs.httpx
  ];

  doCheck = false;

  meta.description = "agentix runtime server";
}
