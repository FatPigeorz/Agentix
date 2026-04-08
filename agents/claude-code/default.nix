{ pkgs ? import <nixpkgs> {} }:

let
  nodejs = pkgs.nodejs_22;
  version = "2.1.96";

  # Fixed-output derivation: npm install with network access.
  # The output hash pins the exact result for reproducibility.
  # Set hash to "" for first build to get the correct hash.
  claude-code-modules = pkgs.stdenv.mkDerivation {
    pname = "claude-code-modules";
    inherit version;

    dontUnpack = true;
    nativeBuildInputs = [ nodejs pkgs.cacert ];

    outputHashMode = "recursive";
    outputHashAlgo = "sha256";
    # Empty string = Nix will print the correct hash on first build
    outputHash = "sha256-WgJ45G8FdxJaEkP2aQMFDx8/PR1LAmFyr+fkRrBgMSU=";

    buildPhase = ''
      export HOME=$TMPDIR
      export npm_config_cache=$TMPDIR/npm-cache
      export SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt

      mkdir -p $out
      npm install -g @anthropic-ai/claude-code@${version} --prefix=$out
    '';

    installPhase = "true";
  };

in
pkgs.stdenv.mkDerivation {
  pname = "claude-code-runtime";
  inherit version;

  dontUnpack = true;

  installPhase = ''
    mkdir -p $out/bin $out/lib

    # Copy node_modules from the FOD
    cp -r ${claude-code-modules}/lib/node_modules $out/lib/

    # Create wrapper script with self-contained paths
    cat > $out/bin/claude <<WRAPPER
    #!/bin/sh
    exec ${nodejs}/bin/node $out/lib/node_modules/@anthropic-ai/claude-code/cli.js "\$@"
    WRAPPER
    chmod +x $out/bin/claude

    # Symlink node + npm for agents that need them
    ln -s ${nodejs}/bin/node $out/bin/node
    ln -s ${nodejs}/bin/npm $out/bin/npm
  '';

  meta.description = "Pre-built Claude Code agent runtime for Harbor";
}
