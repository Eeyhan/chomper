import os
import pickle
import plistlib
import random
import sys
from typing import List

from chomper.const import STACK_ADDRESS, STACK_SIZE
from chomper.exceptions import EmulatorCrashed
from chomper.loader import MachoLoader, Module
from chomper.os import BaseOs

from .fixup import SystemModuleFixup
from .hooks import get_hooks
from .syscall import get_syscall_handlers


# Environment variables
ENVIRON_VARS = r"""SHELL=/bin/sh
PWD=/var/root
LOGNAME=root
HOME=/var/root
LS_COLORS=rs=0:di=01
CLICOLOR=
SSH_CONNECTION=127.0.0.1 59540 127.0.0.1 22
TERM=xterm
USER=root
SHLVL=1
PS1=\h:\w \u\$
SSH_CLIENT=127.0.0.1 59540 22
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/bin/X11:/usr/games
MAIL=/var/mail/root
SSH_TTY=/dev/ttys000
_=/usr/bin/env
SBUS_INSERT_LIBRARIES=/usr/lib/substitute-inserter.dylib
__CF_USER_TEXT_ENCODING=0x0:0:0
CFN_USE_HTTP3=0
CFStringDisableROM=1"""

# Dependent libraries of ObjC
OBJC_DEPENDENCIES = [
    "libsystem_platform.dylib",
    "libsystem_kernel.dylib",
    "libsystem_c.dylib",
    "libsystem_pthread.dylib",
    "libsystem_info.dylib",
    "libsystem_darwin.dylib",
    "libsystem_featureflags.dylib",
    "libsystem_m.dylib",
    "libcorecrypto.dylib",
    "libcommonCrypto.dylib",
    "libcompiler_rt.dylib",
    "libc++abi.dylib",
    "libc++.1.dylib",
    "libmacho.dylib",
    "libdyld.dylib",
    "libobjc.A.dylib",
    "libdispatch.dylib",
    "libsystem_blocks.dylib",
    "libsystem_trace.dylib",
    "libsystem_sandbox.dylib",
    "libsystem_coreservices.dylib",
    "libsystem_notify.dylib",
    "libnetwork.dylib",
    "libicucore.A.dylib",
    "libcache.dylib",
    "libz.1.dylib",
    "libremovefile.dylib",
    "libxpc.dylib",
    "CoreFoundation",
    "CFNetwork",
    "Foundation",
    "Security",
]

# Dependent libraries of UIKit
UI_KIT_DEPENDENCIES = [
    "QuartzCore",
    "BaseBoard",
    "FrontBoardServices",
    "PrototypeTools",
    "TextInput",
    "PhysicsKit",
    "CoreAutoLayout",
    "IOKit",
    "UIFoundation",
    "UIKitServices",
    "UIKitCore",
    "SystemConfiguration",
]

# Define symbolic links in the file system
SYMBOLIC_LINKS = {
    "/usr/share/zoneinfo": "/var/db/timezone/zoneinfo",
    "/var/db/timezone/icutz": "/var/db/timezone/tz/2024a.1.0/icutz/",
    "/var/db/timezone/localtime": "/var/db/timezone/zoneinfo/Asia/Shanghai",
    "/var/db/timezone/tz_latest": " /var/db/timezone/tz/2024a.1.0/",
    "/var/db/timezone/zoneinfo": "/var/db/timezone/tz/2024a.1.0/zoneinfo/",
}

# Default bundle values until an executable with Info.plist is loaded
DEFAULT_BUNDLE_UUID = "43E5FB44-22FC-4DC2-9D9E-E2702A988A2E"
DEFAULT_BUNDLE_IDENTIFIER = "com.sledgeh4w.chomper"
DEFAULT_BUNDLE_EXECUTABLE = "Chomper"


class IosOs(BaseOs):
    """Provide iOS runtime environment."""

    def __init__(self, emu, **kwargs):
        super().__init__(emu, **kwargs)

        self.loader = MachoLoader(emu)

        self.preferences = self._default_preferences.copy()
        self.device_info = self._default_device_info.copy()

        self.proc_id = None
        self.proc_path = None

        self.executable_path = None

    @property
    def _default_preferences(self) -> dict:
        """Define default preferences."""
        return {
            "AppleLanguages": [
                "zh-Hans",
                "en",
            ],
            "AppleLocale": "zh-Hans",
        }

    @property
    def _default_device_info(self) -> dict:
        """Define default device info."""
        return {
            "UserAssignedDeviceName": "iPhone",
            "DeviceName": "iPhone13,1",
            "ProductVersion": "14.2.1",
        }

    @property
    def errno(self) -> int:
        """Get the value of errno."""
        errno = self.emu.find_symbol("_errno")
        return self.emu.read_u32(errno.address)

    @errno.setter
    def errno(self, value: int):
        """Set the value of errno."""
        errno = self.emu.find_symbol("_errno")
        self.emu.write_u32(errno.address, value)

    def _setup_proc_info(self):
        """Initialize process info."""
        self.proc_id = random.randint(10000, 20000)
        self.proc_path = (
            f"/private/var/containers/Bundle/Application"
            f"/{DEFAULT_BUNDLE_UUID}"
            f"/{DEFAULT_BUNDLE_IDENTIFIER}"
            f"/{DEFAULT_BUNDLE_EXECUTABLE}"
        )

    def _setup_hooks(self):
        """Initialize hooks."""
        self.emu.hooks.update(get_hooks())

    def _setup_syscall_handlers(self):
        """Initialize system call handlers."""
        self.emu.syscall_handlers.update(get_syscall_handlers())

    def _setup_kernel_mmio(self):
        """Initialize MMIO used by system libraries."""

        def read_cb(uc, offset, size_, read_ud):
            # arch type
            if offset == 0x23:
                return 0x2
            # vm_page_shift
            elif offset == 0x25:
                return 0xE
            # vm_kernel_page_shift
            elif offset == 0x37:
                return 0xE
            # unknown, appear at _os_trace_init_slow
            elif offset == 0x104:
                return 0x100

            return 0

        def write_cb(uc, offset, size_, value, write_ud):
            pass

        address = 0xFFFFFC000
        size = 0x1000

        self.emu.uc.mmio_map(address, size, read_cb, None, write_cb, None)

    def _construct_environ(self) -> int:
        """Construct a structure that contains environment variables."""
        lines = ENVIRON_VARS.split("\n")

        size = self.emu.arch.addr_size * (len(lines) + 1)
        buffer = self.emu.create_buffer(size)

        for index, line in enumerate(lines):
            address = buffer + self.emu.arch.addr_size * index
            self.emu.write_pointer(address, self.emu.create_string(line))

        self.emu.write_pointer(buffer + size - self.emu.arch.addr_size, 0)

        return buffer

    def _init_program_vars(self):
        """Initialize program variables, works like `__program_vars_init`."""
        argc = self.emu.create_buffer(8)
        self.emu.write_int(argc, 0, 8)

        nx_argc_pointer = self.emu.find_symbol("_NXArgc_pointer")
        self.emu.write_pointer(nx_argc_pointer.address, argc)

        nx_argv_pointer = self.emu.find_symbol("_NXArgv_pointer")
        self.emu.write_pointer(nx_argv_pointer.address, self.emu.create_string(""))

        environ = self.emu.create_buffer(8)
        self.emu.write_pointer(environ, self._construct_environ())

        environ_pointer = self.emu.find_symbol("_environ_pointer")
        self.emu.write_pointer(environ_pointer.address, environ)

        progname_pointer = self.emu.find_symbol("___progname_pointer")
        self.emu.write_pointer(progname_pointer.address, self.emu.create_string(""))

    def _init_dyld_vars(self):
        """Initialize global variables in `libdyld.dylib`."""
        g_use_dyld3 = self.emu.find_symbol("_gUseDyld3")
        self.emu.write_u8(g_use_dyld3.address, 1)

        dyld_all_images = self.emu.find_symbol("__ZN5dyld310gAllImagesE")

        # dyld3::closure::ContainerTypedBytes::findAttributePayload
        attribute_payload_ptr = self.emu.create_buffer(8)

        self.emu.write_u32(attribute_payload_ptr, 2**10)
        self.emu.write_u8(attribute_payload_ptr + 4, 0x20)

        self.emu.write_pointer(dyld_all_images.address, attribute_payload_ptr)

        # dyld3::AllImages::platform
        platform_ptr = self.emu.create_buffer(0x144)
        self.emu.write_u32(platform_ptr + 0x140, 2)

        self.emu.write_pointer(dyld_all_images.address + 0x50, platform_ptr)

    def _init_lib_system_kernel(self):
        """Initialize `libsystem_kernel.dylib`."""
        self.emu.call_symbol("_mach_init_doit")

    def _init_lib_system_pthread(self):
        """Initialize `libsystem_pthread.dylib`."""
        main_thread = self.emu.create_buffer(256)

        self.emu.write_pointer(main_thread + 0xB0, STACK_ADDRESS)
        self.emu.write_pointer(main_thread + 0xE0, STACK_ADDRESS + STACK_SIZE)

        main_thread_ptr = self.emu.find_symbol("__main_thread_ptr")
        self.emu.write_pointer(main_thread_ptr.address, main_thread)

    def _init_lib_xpc(self):
        """Initialize `libxpc.dylib`."""
        try:
            self.emu.call_symbol("__libxpc_initializer")
        except EmulatorCrashed:
            pass

    def _init_objc_vars(self):
        """Initialize global variables in `libobjc.A.dylib
        while calling `__objc_init`."""
        prototypes = self.emu.find_symbol("__ZL10prototypes")
        self.emu.write_u64(prototypes.address, 0)

        gdb_objc_realized_classes = self.emu.find_symbol("_gdb_objc_realized_classes")
        protocolsv_ret = self.emu.call_symbol("__ZL9protocolsv")

        self.emu.write_pointer(gdb_objc_realized_classes.address, protocolsv_ret)

        opt = self.emu.find_symbol("__ZL3opt")
        self.emu.write_pointer(opt.address, 0)

        # Disable pre-optimization
        disable_preopt = self.emu.find_symbol("_DisablePreopt")
        self.emu.write_u8(disable_preopt.address, 1)

        self.emu.call_symbol("__objc_init")

    def init_objc(self, module: Module):
        """Initialize Objective-C for the module.

        Calling `map_images` and `load_images` of `libobjc.A.dylib`.
        """
        if not module.binary or module.image_base is None:
            return

        if not self.emu.find_module("libobjc.A.dylib"):
            return

        text_segment = module.binary.get_segment("__TEXT")

        mach_header_ptr = module.base - module.image_base + text_segment.virtual_address
        mach_header_ptrs = self.emu.create_buffer(self.emu.arch.addr_size)

        mh_execute_header_pointer = self.emu.find_symbol("__mh_execute_header_pointer")

        self.emu.write_pointer(mach_header_ptrs, mach_header_ptr)
        self.emu.write_pointer(mh_execute_header_pointer.address, mach_header_ptr)

        try:
            self.emu.call_symbol("_map_images", 1, 0, mach_header_ptrs)
            self.emu.call_symbol("_load_images", 0, mach_header_ptr)
        except EmulatorCrashed:
            self.emu.logger.warning("Initialize Objective-C failed.")

            # Release locks
            runtime_lock = self.emu.find_symbol("_runtimeLock")
            self.emu.write_u64(runtime_lock.address, 0)

            lcl_rwlock = self.emu.find_symbol("_lcl_rwlock")
            self.emu.write_u64(lcl_rwlock.address, 0)

    def search_module_binary(self, module_name: str) -> str:
        """Search system module binary in rootfs directory.

        raises:
            FileNotFoundError: If module not found.
        """
        lib_dirs = [
            "usr/lib/system",
            "usr/lib",
            "System/Library/Frameworks",
            "System/Library/PrivateFrameworks",
        ]

        for lib_dir in lib_dirs:
            path = os.path.join(self.rootfs_path or ".", lib_dir)

            lib_path = os.path.join(path, module_name)
            if os.path.exists(lib_path):
                return lib_path

            framework_path = os.path.join(path, f"{module_name}.framework")
            if os.path.exists(framework_path):
                return os.path.join(framework_path, module_name)

        raise FileNotFoundError("Module '%s' not found" % module_name)

    def resolve_modules(self, module_names: List[str]):
        """Load system modules if don't loaded."""
        fixup = SystemModuleFixup(self.emu)

        for module_name in module_names:
            if self.emu.find_module(module_name):
                continue

            module_file = self.search_module_binary(module_name)
            module = self.emu.load_module(
                module_file=module_file,
                exec_objc_init=False,
            )

            # Fixup must be executed before initializing Objective-C.
            fixup.install(module)

            self._after_module_loaded(module_name)

            self.init_objc(module)

            module.binary = None

    def _after_module_loaded(self, module_name: str):
        """Perform initialization after module loaded."""
        if module_name == "libsystem_kernel.dylib":
            self._init_lib_system_kernel()
        elif module_name == "libsystem_c.dylib":
            self._init_program_vars()
        elif module_name == "libdyld.dylib":
            self._init_dyld_vars()
        elif module_name == "libsystem_pthread.dylib":
            self._init_lib_system_pthread()
        elif module_name == "libobjc.A.dylib":
            self._init_objc_vars()

    def _enable_objc(self):
        """Enable Objective-C support."""
        self.resolve_modules(OBJC_DEPENDENCIES)

        self._init_lib_xpc()

        # Call initialize function of `CoreFoundation`
        self.emu.call_symbol("___CFInitialize")

        is_cf_prefs_d = self.emu.find_symbol("_isCFPrefsD")
        self.emu.write_u8(is_cf_prefs_d.address, 1)

        # Call initialize function of `Foundation`
        self.emu.call_symbol("__NSInitializePlatform")

        self.fix_method_signature_rom_table()

        amkrtemp_sentinel = self.emu.find_symbol("__amkrtemp.sentinel")
        self.emu.write_pointer(amkrtemp_sentinel.address, self.emu.create_string(""))

    def _enable_ui_kit(self):
        """Enable UIKit support.

        Mainly used to load `UIDevice` class, which is used to get device info.
        """
        self.resolve_modules(UI_KIT_DEPENDENCIES)

    def _setup_symbolic_links(self):
        """Setup symbolic links."""
        for src, dst in SYMBOLIC_LINKS.items():
            self.file_system.set_symbolic_link(src, dst)

    def _setup_bundle_dir(self):
        """Setup bundle directory."""
        bundle_path = os.path.dirname(self.proc_path)
        container_path = os.path.dirname(bundle_path)

        self.file_system.set_working_dir(bundle_path)

        local_container_path = os.path.join(
            self.rootfs_path,
            "private",
            "var",
            "containers",
            "Bundle",
            "Application",
            DEFAULT_BUNDLE_UUID,
        )
        local_bundle_path = os.path.join(
            local_container_path, DEFAULT_BUNDLE_IDENTIFIER
        )

        self.file_system.forward_path(container_path, local_container_path)
        self.file_system.forward_path(bundle_path, local_bundle_path)

    def set_main_executable(self, executable_path: str):
        """Set main executable path."""
        self.executable_path = executable_path

        bundle_path = os.path.dirname(self.proc_path)
        container_path = os.path.dirname(bundle_path)

        executable_dir = os.path.dirname(self.executable_path)
        info_path = os.path.join(executable_dir, "Info.plist")

        if os.path.exists(info_path):
            with open(info_path, "rb") as f:
                info_data = plistlib.load(f)

            bundle_identifier = info_data["CFBundleIdentifier"]
            bundle_executable = info_data["CFBundleExecutable"]

            bundle_path = f"{container_path}/{bundle_identifier}"
            self.proc_path = f"{bundle_path}/{bundle_executable}"

            self._setup_bundle_dir()

            cf_progname = self.emu.find_symbol("___CFprogname")
            cf_process_path = self.emu.find_symbol("___CFProcessPath")

            cf_progname_str = self.emu.create_string(self.proc_path.split("/")[-1])
            cf_process_path_str = self.emu.create_string(self.proc_path)

            self.emu.write_pointer(cf_progname.address, cf_progname_str)
            self.emu.write_pointer(cf_process_path.address, cf_process_path_str)

        # Set path forwarding to executable and Info.plist
        self.file_system.forward_path(
            src_path=self.proc_path,
            dst_path=self.executable_path,
        )
        self.file_system.forward_path(
            src_path=f"{bundle_path}/Info.plist",
            dst_path=info_path,
        )

    def fix_method_signature_rom_table(self):
        """Fix MethodSignatureROMTable by using pre dumped data file."""
        if sys.version_info >= (3, 9):
            import importlib.resources

            data_path = importlib.resources.files(__package__.split(".")[0]) / "res"
        else:
            import pkg_resources

            data_path = pkg_resources.resource_filename(
                __package__.split(".")[0], "res"
            )

        with open(os.path.join(data_path, "method_signature_rom_table.pkl"), "rb") as f:
            table_data = pickle.load(f)

        table = self.emu.find_symbol("_MethodSignatureROMTable")

        for index, item in enumerate(table_data):
            offset = table.address + index * 24
            str_ptr = self.emu.create_string(item[1])

            self.emu.write_pointer(offset + 8, str_ptr)
            self.emu.write_u64(offset + 16, item[2])

    def _fd_open(self, fd: int, mode: str, unbuffered: bool = False) -> int:
        mode_p = self.emu.create_string(mode)

        try:
            fp = self.emu.call_symbol("_fdopen", fd, mode_p)
            flags = self.emu.read_u32(fp + 16)

            if unbuffered:
                flags |= 0x2

            self.emu.write_u32(fp + 16, flags)
            return fp
        finally:
            self.emu.free(mode_p)

    def _setup_standard_io(self):
        """Setup standard IO: `stdin`, `stdout`, `stderr`."""
        stdin_p = self.emu.find_symbol("___stdinp")
        stdout_p = self.emu.find_symbol("___stdoutp")
        stderr_p = self.emu.find_symbol("___stderrp")

        if isinstance(self.file_system.stdin_fd, int):
            stdin_fp = self._fd_open(self.file_system.stdin_fd, "r")
            self.emu.write_pointer(stdin_p.address, stdin_fp)

        stdout_fp = self._fd_open(self.file_system.stdout_fd, "w", unbuffered=True)
        self.emu.write_pointer(stdout_p.address, stdout_fp)

        stderr_fp = self._fd_open(self.file_system.stderr_fd, "w", unbuffered=True)
        self.emu.write_pointer(stderr_p.address, stderr_fp)

    def initialize(self):
        """Initialize environment."""
        self._setup_hooks()
        self._setup_syscall_handlers()

        self._setup_kernel_mmio()

        self._setup_proc_info()
        self._setup_symbolic_links()
        self._setup_bundle_dir()

        if self.emu.enable_objc:
            self._enable_objc()

        if self.emu.enable_ui_kit:
            self._enable_ui_kit()

        self._setup_standard_io()
