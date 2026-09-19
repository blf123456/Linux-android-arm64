#!/usr/bin/env bash
# Sourced by build_all.sh. Cloud mode uses the same Bazel/Legacy functions as
# local builds. Never synthesize a separate .config for cloud builds.

cloud_main() {
    if [[ $# -ne 1 ]]; then
        log_error 'Usage: bash build_all.sh --cloud <kernel-version>'
        return 2
    fi
    local version="$1" branch
    case "$version" in
        6.18-Android17) branch=android17-6.18 ;;
        6.12-Android16) branch=android16-6.12 ;;
        6.6-Android15)  branch=android15-6.6 ;;
        6.1-Android14)  branch=android14-6.1 ;;
        5.15-Android13) branch=android13-5.15 ;;
        5.10-Android13) branch=android13-5.10 ;;
        5.10-Android12) branch=android12-5.10 ;;
        *) log_error "Unsupported kernel version: $version"; return 2 ;;
    esac
    local jobs="${JOBS:-$(nproc)}"
    [[ "$jobs" =~ ^[1-9][0-9]*$ ]] || { log_error 'JOBS must be positive'; return 2; }
    STRIP_CHOICE="${STRIP_CHOICE:-n}"
    [[ "$STRIP_CHOICE" =~ ^[yYnN]$ ]] || { log_error 'STRIP_CHOICE must be y or n'; return 2; }
    local artifact_dir="${ARTIFACT_DIR:-$BUILD_ROOT/artifacts/$version}"
    local kernel_dir="$KERNELS_ROOT/$version"
    mkdir -p "$kernel_dir" "$artifact_dir"
    artifact_dir="$(cd "$artifact_dir" && pwd)"
    trap cleanup_driver_build_on_exit EXIT
    clean_driver_build
    rm -f -- "$DRIVER_SRC/lsdriver.ko" "$DRIVER_SRC/$version.ko"

    cd "$kernel_dir"
    log_info "Syncing official build environment: common-$branch"
    curl --fail --location --retry 5 --retry-all-errors \
        https://storage.googleapis.com/git-repo-downloads/repo -o repo
    chmod +x repo
    # Kleaf/Legacy need build scripts, Bazel, hermetic host tools, sysroots and
    # (where configured) Rust. Sync the manifest, not only common/ and Clang.
    ./repo init -u https://android.googlesource.com/kernel/manifest \
        -b "${MANIFEST_REVISION:-common-$branch}" --depth=1 --no-clone-bundle
    ./repo sync -c --no-tags --no-clone-bundle --fail-fast -j"$jobs"
    ./repo manifest -r -o "$artifact_dir/manifest.xml"

    # Compiler paths/revisions and make arguments are shared with local builds.
    # Missing author toolchains are errors, never silent compiler fallbacks.
    CLOUD_ARTIFACT_DIR="$artifact_dir"
    build_selected_kernel "$version"

    local kernel_out method
    if [[ "$version" == 5.10-Android12 ]]; then
        method=legacy
        kernel_out="$kernel_dir/out/$version/common/common"
    else
        method=bazel
        kernel_out="$(readlink -f "$kernel_dir/bazel-bin/common/kernel_aarch64")"
    fi
    test -s "$DRIVER_SRC/$version.ko"
    cp "$DRIVER_SRC/$version.ko" "$artifact_dir/"
    cp "$kernel_out/.config" "$artifact_dir/kernel.config"
    cp "$kernel_out/Module.symvers" "$artifact_dir/kernel-Module.symvers"
    {
        printf 'driver_commit=%s\n' "$(git -C "$BUILD_ROOT" rev-parse HEAD)"
        printf 'build_method=%s\n' "$method"
        printf 'kernel_branch=%s\n' "$branch"
        printf 'kernel_commit=%s\n' "$(git -C "$kernel_dir/common" rev-parse HEAD)"
        printf 'manifest_commit=%s\n' "$(git -C "$kernel_dir/.repo/manifests" rev-parse HEAD)"
        printf 'strip_requested=%s\n' "$STRIP_CHOICE"
        modinfo "$DRIVER_SRC/$version.ko"
    } > "$artifact_dir/build-info.txt"
    python3 "$BUILD_ROOT/.github/scripts/module-audit.py" \
        "$artifact_dir/$version.ko" --kernel "$version" \
        --reference "$BUILD_ROOT/.github/reference/upstream-modules.json" \
        --config "$artifact_dir/kernel.config" \
        --output "$artifact_dir/module-audit.json"
    (
        cd "$artifact_dir"
        sha256sum "$version.ko" > SHA256SUMS
    )
    log_info "Cloud build completed using $method: $artifact_dir/$version.ko"
}
