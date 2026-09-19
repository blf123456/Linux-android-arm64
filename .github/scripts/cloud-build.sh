#!/usr/bin/env bash
# Sourced by build_all.sh --cloud. All downloads/builds happen on the Actions runner.

cloud_main() {
    if [[ $# -ne 1 ]]; then
        log_error 'Usage: bash build_all.sh --cloud <kernel-version>'
        return 2
    fi

    local version="$1" branch preferred_clang
    case "$version" in
        6.18-Android17) branch=android17-6.18; preferred_clang=clang-r584948c ;;
        6.12-Android16) branch=android16-6.12; preferred_clang=clang-r536225 ;;
        6.6-Android15)  branch=android15-6.6;  preferred_clang=clang-r510928 ;;
        6.1-Android14)  branch=android14-6.1;  preferred_clang=clang-r487747c ;;
        5.15-Android13) branch=android13-5.15; preferred_clang=clang-r450784e ;;
        5.10-Android13) branch=android13-5.10; preferred_clang=clang-r450784e ;;
        5.10-Android12) branch=android12-5.10; preferred_clang=clang-r416183b ;;
        *) log_error "Unsupported kernel version: $version"; return 2 ;;
    esac

    local jobs="${JOBS:-$(nproc)}"
    [[ "$jobs" =~ ^[1-9][0-9]*$ ]] || { log_error 'JOBS must be positive'; return 2; }
    STRIP_CHOICE="${STRIP_CHOICE:-n}"
    [[ "$STRIP_CHOICE" =~ ^[yYnN]$ ]] || { log_error 'STRIP_CHOICE must be y or n'; return 2; }
    local artifact_dir="${ARTIFACT_DIR:-$BUILD_ROOT/artifacts/$version}"
    local kernel_dir="$KERNELS_ROOT/$version"
    mkdir -p "$kernel_dir" "$artifact_dir"
    trap cleanup_driver_build_on_exit EXIT
    clean_driver_build
    # Never accept an old module as the result of this build.
    rm -f -- "$DRIVER_SRC/lsdriver.ko" "$DRIVER_SRC/$version.ko"

    cd "$kernel_dir"
    log_info "Fetching official Android manifest: common-$branch"
    curl --fail --location --retry 5 --retry-all-errors \
        https://storage.googleapis.com/git-repo-downloads/repo -o repo
    chmod +x repo
    ./repo init -u https://android.googlesource.com/kernel/manifest \
        -b "common-$branch" --depth=1 --no-clone-bundle
    ./repo manifest -o manifest.xml

    # Sync only the kernel. Userspace/UI submodules and complete Android trees are unnecessary.
    ./repo sync -c --no-tags --no-clone-bundle --fail-fast -j"$jobs" common

    # Resolve the compiler revision from this kernel's official manifest. Sparse checkout
    # avoids downloading every compiler version shipped by the prebuilt repository.
    python3 "$BUILD_ROOT/.github/scripts/clang-manifest.py" manifest.xml > clang.env
    # Values are shell-quoted by clang-manifest.py.
    source clang.env
    local clang_checkout="$kernel_dir/toolchain"
    mkdir -p "$clang_checkout"
    git -C "$clang_checkout" init
    git -C "$clang_checkout" remote add origin "$CLANG_REMOTE"
    git -C "$clang_checkout" sparse-checkout init --cone
    git -C "$clang_checkout" -c http.version=HTTP/1.1 fetch \
        --depth=1 --filter=blob:none origin "$CLANG_REVISION"

    local clang_version="$preferred_clang" configured_clang
    configured_clang="$(sed -nE 's/^CLANG_VERSION=["\x27]?([^"\x27[:space:]]+).*/\1/p' \
        common/build.config.constants common/build.config.common 2>/dev/null | head -n 1 || true)"
    if ! git -C "$clang_checkout" cat-file -e "FETCH_HEAD:$clang_version" 2>/dev/null; then
        clang_version="${configured_clang:+clang-$configured_clang}"
        if [[ -z "$clang_version" ]] || ! git -C "$clang_checkout" cat-file -e "FETCH_HEAD:$clang_version" 2>/dev/null; then
            log_error "Compiler $preferred_clang is absent from the official manifest revision."
            git -C "$clang_checkout" ls-tree --name-only FETCH_HEAD
            return 1
        fi
    fi
    git -C "$clang_checkout" sparse-checkout set "$clang_version"
    git -C "$clang_checkout" checkout --detach FETCH_HEAD
    local clang_path="$clang_checkout/$clang_version"
    [[ -x "$clang_path/bin/clang" ]] || { log_error 'Clang download is incomplete'; return 1; }
    export PATH="$clang_path/bin:$PATH"
    clang --version

    local kernel_out="$kernel_dir/out"
    local make_args=(
        -C "$kernel_dir/common" O="$kernel_out" ARCH=arm64
        LLVM=1 LLVM_IAS=1 CROSS_COMPILE=aarch64-linux-gnu-
        HOSTCC=clang HOSTCXX=clang++ HOSTLD=ld.lld
    )
    log_info "Preparing GKI module build environment: $version"
    make "${make_args[@]}" gki_defconfig
    # Keep the GKI CFI/LTO configuration. This C-only, no-CRC module does not need
    # Rust, BTF, a complete vmlinux link or the kernel's Module.symvers.
    "$kernel_dir/common/scripts/config" --file "$kernel_out/.config" \
        -d LOCALVERSION_AUTO -d DEBUG_INFO_BTF -d DEBUG_INFO_BTF_MODULES \
        -d RUST -d EXTENDED_MODVERSIONS -e MODVERSIONS -e BASIC_MODVERSIONS
    make "${make_args[@]}" olddefconfig
    make "${make_args[@]}" -j"$jobs" modules_prepare

    local make_status=0
    make "${make_args[@]}" M="$DRIVER_SRC" \
        KBUILD_MODPOST_WARN=1 CONFIG_EXTENDED_MODVERSIONS=n \
        CONFIG_DEBUG_INFO_BTF_MODULES= -j"$jobs" modules || make_status=$?
    if [[ "$make_status" -ne 0 ]] && fix_empty_ext_modversions; then
        make_status=0
        make "${make_args[@]}" M="$DRIVER_SRC" \
            KBUILD_MODPOST_WARN=1 CONFIG_EXTENDED_MODVERSIONS=n \
            CONFIG_DEBUG_INFO_BTF_MODULES= -j"$jobs" modules || make_status=$?
    fi
    [[ "$make_status" -eq 0 ]] || return "$make_status"
    handle_output "$version" "$clang_path"
    test -s "$DRIVER_SRC/$version.ko"
    llvm-readelf -h "$DRIVER_SRC/$version.ko" | grep -q 'Machine:.*AArch64'
    cp "$DRIVER_SRC/$version.ko" "$artifact_dir/"
    cp "$kernel_out/.config" "$artifact_dir/kernel.config"
    {
        printf 'driver_commit=%s\n' "$(git -C "$BUILD_ROOT" rev-parse HEAD)"
        printf 'kernel_branch=%s\n' "$branch"
        printf 'kernel_commit=%s\n' "$(git -C "$kernel_dir/common" rev-parse HEAD)"
        printf 'clang_commit=%s\n' "$(git -C "$clang_checkout" rev-parse HEAD)"
        printf 'clang_version=%s\n' "$clang_version"
        printf 'strip_requested=%s\n' "$STRIP_CHOICE"
        modinfo "$DRIVER_SRC/$version.ko"
    } > "$artifact_dir/build-info.txt"
    (
        cd "$artifact_dir"
        sha256sum "$version.ko" > SHA256SUMS
    )
    log_info "Cloud build completed: $artifact_dir/$version.ko"
}
