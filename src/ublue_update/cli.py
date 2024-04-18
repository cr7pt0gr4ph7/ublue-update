import os
import subprocess
import logging
import argparse

from ublue_update.update_checks.system import (
    system_update_check,
    pending_deployment_check,
)
from ublue_update.update_checks.wait import transaction_wait
from ublue_update.update_inhibitors.hardware import check_hardware_inhibitors
from ublue_update.config import cfg
from ublue_update.session import get_xdg_runtime_dir, get_active_sessions
from ublue_update.filelock import acquire_lock, release_lock


def notify(title: str, body: str, actions: list = [], urgency: str = "normal"):
    if not cfg.dbus_notify:
        return
    process_uid = os.getuid()
    args = [
        "/usr/bin/notify-send",
        title,
        body,
        "--app-name=Universal Blue Updater",
        "--icon=software-update-available-symbolic",
        f"--urgency={urgency}",
    ]
    if actions != []:
        for action in actions:
            args.append(f"--action={action}")
    if process_uid == 0:
        users = []
        try:
            users = get_active_sessions()
        except KeyError as e:
            log.error("failed to get active logind session info", e)
        for user in users:
            try:
                xdg_runtime_dir = get_xdg_runtime_dir(user["User"])
            except KeyError as e:
                log.error(f"failed to get xdg_runtime_dir for user: {user['Name']}", e)
                return
            user_args = [
                "/usr/bin/sudo",
                "-u",
                f"{user['Name']}",
                "DISPLAY=:0",
                f"DBUS_SESSION_BUS_ADDRESS=unix:path={xdg_runtime_dir}/bus",
            ]
            user_args += args
            out = subprocess.run(user_args, capture_output=True)
            if actions != []:
                return out
        return
    out = subprocess.run(args, capture_output=True)
    return out


def ask_for_updates(system):
    if not cfg.dbus_notify:
        return
    out = notify(
        "System Updater",
        "Update available, but system checks failed. Update now?",
        ["universal-blue-update-confirm=Confirm"],
        "critical",
    )
    if out is None:
        return
    # if the user has confirmed
    if "universal-blue-update-confirm" in out.stdout.decode("utf-8"):
        run_updates(system, True)


def hardware_inhibitor_checks_failed(
    failures: list, hardware_check: bool, system_update_available: bool, system: bool
):
    # ask if an update can be performed through dbus notifications
    if system_update_available and not hardware_check:
        log.info("Harware checks failed, but update is available")
        ask_for_updates(system)
    # notify systemd that the checks have failed,
    # systemd will try to rerun the unit
    exception_log = "\n - ".join(failures)
    raise Exception(f"update failed to pass checks: \n - {exception_log}")


def run_updates(system, system_update_available):
    process_uid = os.getuid()
    filelock_path = "/run/ublue-update.lock"
    if process_uid != 0:
        xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        if os.path.isdir(xdg_runtime_dir):
            filelock_path = f"{xdg_runtime_dir}/ublue-update.lock"
    fd = acquire_lock(filelock_path)
    if fd is None:
        raise Exception("updates are already running for this user")

    """Wait on any existing transactions to complete before updating"""
    transaction_wait()

    if process_uid == 0:
        notify(
            "System Updater",
            "System passed checks, updating ...",
        )
        users = []
        try:
            users = get_active_sessions()
        except KeyError as e:
            log.error("failed to get active logind session info", e)

        if system:
            users = []

        """System"""
        out = subprocess.run(
            [
                "/usr/bin/topgrade",
                "--config",
                "/usr/share/ublue-update/topgrade-system.toml",
            ],
            capture_output=True,
        )
        log.debug(out.stdout.decode("utf-8"))

        if out.returncode != 0:
            print(
                f"topgrade returned code {out.returncode}, program output:"
            )
            print(out.stdout.decode("utf-8"))
            os._exit(out.returncode)

        """Users"""
        for user in users:
            try:
                xdg_runtime_dir = get_xdg_runtime_dir(user["User"])
            except KeyError as e:
                log.error(f"failed to get xdg_runtime_dir for user: {user['Name']}", e)
                break
            log.info(
                f"""Running update for user: '{user['Name']}'"""
            )

            out = subprocess.run(
                [
                    "/usr/bin/sudo",
                    "-u",
                    f"{user['Name']}",
                    "DISPLAY=:0",
                    f"XDG_RUNTIME_DIR={xdg_runtime_dir}",
                    f"DBUS_SESSION_BUS_ADDRESS=unix:path={xdg_runtime_dir}/bus",
                    "/usr/bin/topgrade",
                    "--config",
                    "/usr/share/ublue-update/topgrade-user.toml",
                ],
                capture_output=True,
            )
            log.debug(out.stdout.decode("utf-8"))
        log.info("System update complete")
        if pending_deployment_check() and system_update_available and cfg.dbus_notify:
            out = notify(
                "System Updater",
                "System update complete, pending changes will take effect after reboot. Reboot now?",
                ["universal-blue-update-reboot=Reboot Now"],
            )
            # if the user has confirmed the reboot
            if "universal-blue-update-reboot" in out.stdout.decode("utf-8"):
                subprocess.run(["systemctl", "reboot"])
    else:
        if system:
            raise Exception(
                "ublue-update needs to be run as root to perform system updates!"
            )
    release_lock(fd)
    os._exit(0)


# setup logging
logging.basicConfig(
    format="[%(asctime)s] %(name)s:%(levelname)s | %(message)s",
    level=os.getenv("UBLUE_LOG", default="INFO").upper(),
)
log = logging.getLogger(__name__)


def main():

    # setup argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="force manual update, skipping update checks",
    )
    parser.add_argument(
        "-c", "--check", action="store_true", help="run update checks and exit"
    )
    parser.add_argument(
        "-u",
        "--updatecheck",
        action="store_true",
        help="check for updates and exit",
    )
    parser.add_argument(
        "-w",
        "--wait",
        action="store_true",
        help="wait for transactions to complete and exit",
    )
    parser.add_argument(
        "--config",
        help="use the specified config file"
    )
    parser.add_argument(
        "--system",
        action="store_true",
        help="only run system updates (requires root)",
    )
    cli_args = parser.parse_args()

    # Load the configuration file
    cfg.load_config(cli_args.config)

    if cli_args.wait:
        transaction_wait()
        os._exit(0)

    system_update_available: bool = system_update_check()
    if not cli_args.force and not cli_args.updatecheck:
        hardware_checks_failed, failures = check_hardware_inhibitors()
        if hardware_checks_failed:
            hardware_inhibitor_checks_failed(
                failures,
                cli_args.check,
                system_update_available,
                cli_args.system,
            )
        if cli_args.check:
            os._exit(0)

    if cli_args.updatecheck:
        if not system_update_available:
            raise Exception("Update not available")
        os._exit(0)

    # system checks passed
    log.info("System passed all update checks")
    run_updates(cli_args.system, system_update_available)
