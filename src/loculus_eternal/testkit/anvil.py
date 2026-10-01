"""Start and drive a local anvil chain with the LoculusEternal contract deployed.

anvil runs with one slot per epoch so that the finalized block is always two behind the
latest, which lets tests exercise "wait for finality" without waiting. The hardfork is left
at anvil's default (the latest it knows), because blob transactions must carry the sidecar
shape of the current network, not an older one.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from eth_account import Account
from web3 import Web3

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_DIR = ROOT / "contracts"
ARTIFACT = CONTRACTS_DIR / "out" / "LoculusEternal.sol" / "LoculusEternal.json"

# anvil's well-known development mnemonic. Keys are derived from it rather than written here.
DEV_MNEMONIC = "test test test test test test test test test test test junk"


def preconditions_met() -> bool:
    return shutil.which("anvil") is not None and shutil.which("forge") is not None


def dev_account(index: int):
    Account.enable_unaudited_hdwallet_features()
    return Account.from_mnemonic(DEV_MNEMONIC, account_path=f"m/44'/60'/0'/0/{index}")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def load_artifact() -> dict:
    """The forge build output; builds the contracts if it is missing."""
    if not ARTIFACT.exists():
        subprocess.run(["forge", "build"], cwd=CONTRACTS_DIR, check=True, capture_output=True)
    return json.loads(ARTIFACT.read_text())


class Anvil:
    """A running anvil process, a web3 connection, and the deployed contract.

    Account 0 deploys; account 1 is the publisher. Both are funded by anvil.
    """

    def __init__(self, hardfork: str | None = None, chain_id: int = 31337):
        self.port = _free_port()
        self.chain_id = chain_id
        args = ["anvil", "--port", str(self.port), "--chain-id", str(chain_id), "--slots-in-an-epoch", "1", "--silent"]
        if hardfork:
            args += ["--hardfork", hardfork]
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.url = f"http://127.0.0.1:{self.port}"
        self.w3 = Web3(Web3.HTTPProvider(self.url, request_kwargs={"timeout": 30}))
        self._wait_ready()
        self.deployer = dev_account(0)
        self.publisher = dev_account(1)
        self.contract = self._deploy()

    def _wait_ready(self, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.w3.is_connected() and self.w3.eth.chain_id == self.chain_id:
                    return
            except Exception:
                pass
            time.sleep(0.1)
        self.close()
        raise RuntimeError("anvil did not start")

    def _deploy(self):
        artifact = load_artifact()
        factory = self.w3.eth.contract(abi=artifact["abi"], bytecode=artifact["bytecode"]["object"])
        tx = factory.constructor(self.publisher.address).build_transaction(
            {"from": self.deployer.address, "nonce": self.w3.eth.get_transaction_count(self.deployer.address), "chainId": self.chain_id}
        )
        signed = self.deployer.sign_transaction(tx)
        receipt = self.w3.eth.wait_for_transaction_receipt(self.w3.eth.send_raw_transaction(signed.raw_transaction))
        return self.w3.eth.contract(address=receipt.contractAddress, abi=artifact["abi"])

    # --- chain control --------------------------------------------------------------------

    def rpc(self, method: str, params: list | None = None):
        return self.w3.provider.make_request(method, params or [])

    def mine(self, blocks: int = 1) -> None:
        self.rpc("anvil_mine", [hex(blocks)])

    def warp(self, seconds: int) -> None:
        """Advance chain time and mine one block so the new time is observable."""
        self.rpc("evm_increaseTime", [seconds])
        self.mine(1)

    def set_automine(self, enabled: bool) -> None:
        self.rpc("evm_setAutomine", [enabled])

    def fund(self, address: str, wei: int) -> None:
        self.rpc("anvil_setBalance", [address, hex(wei)])

    def finalized_block_number(self) -> int:
        return self.w3.eth.get_block("finalized")["number"]

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
