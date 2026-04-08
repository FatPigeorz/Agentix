{
  description = "harbor-nix: Coding Agent SDK — Nix-based agent packaging and sandboxed execution";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      python = pkgs.python312;
    in
    {
      # ── Packages ────────────────────────────────────────────────
      packages.${system} = {
        runtime     = import ./runtime/default.nix { inherit pkgs; };
        claude-code = import ./agents/claude-code/default.nix { inherit pkgs; };
      };

      # ── Dev shell: nix develop ──────────────────────────────────
      # Everything you need to develop, lint, test, and build.
      devShells.${system}.default = pkgs.mkShell {
        packages = [
          # Python
          python
          pkgs.uv

          # Linting & formatting
          pkgs.ruff

          # Testing
          python.pkgs.pytest
          python.pkgs.pytest-asyncio

          # Runtime deps (for local dev without Nix build)
          python.pkgs.fastapi
          python.pkgs.uvicorn
          python.pkgs.pydantic
          python.pkgs.python-multipart
          python.pkgs.httpx

          # Tools
          pkgs.nodejs_22
          pkgs.docker
        ];

        shellHook = ''
          echo "harbor-nix dev shell"
          echo "  python: $(python3 --version)"
          echo "  uv:     $(uv --version)"
          echo "  ruff:   $(ruff --version)"
          echo ""
          echo "Commands:"
          echo "  uv sync                    # install deps"
          echo "  ruff check runtime/        # lint"
          echo "  ruff format runtime/       # format"
          echo "  pytest                     # test"
          echo "  python -m hnix             # run runtime server locally"
          echo "  nix build .#runtime        # build runtime closure"
          echo "  nix build .#claude-code    # build agent closure"
        '';
      };
    };
}
