"""The short form of the recovery command: a contract address and an RPC on the command line,
public archives built in, and the reader's own sources added with a flag."""

from pathlib import Path

import pytest

from loculus_eternal import cli
from loculus_eternal import config as configuration
from loculus_eternal.config import PUBLIC_RPC_EXAMPLE, PUBLIC_SOURCES, build_sources, recovery_sources, source_from_flag

ADDRESS = "0x7b19365a0e811901Cb53aaA8bfaA82854c46914D"


def test_R17_a_source_flag_is_a_type_and_its_url_or_directory():
    assert source_from_flag("beacon=http://localhost:5052") == {"type": "beacon", "endpoints": ["http://localhost:5052"]}
    assert source_from_flag("ipfs=http://127.0.0.1:5001") == {"type": "ipfs", "endpoints": ["http://127.0.0.1:5001"]}
    assert source_from_flag("blob-archiver=https://hub.example") == {"type": "blob-archiver", "url": "https://hub.example"}
    assert source_from_flag("local=/mnt/blobs") == {"type": "local", "path": "/mnt/blobs"}
    # A URL keeps everything after the first equals sign, query strings included.
    assert source_from_flag("blobscan=https://a.example/api?key=1")["url"] == "https://a.example/api?key=1"
    # The two archives with a known public instance may be named alone.
    assert source_from_flag("blockscout") == {"type": "blockscout"} and source_from_flag("blobscan") == {"type": "blobscan"}
    with pytest.raises(SystemExit, match="unknown source type 'beacons'.*known: beacon"):
        source_from_flag("beacons=http://x")
    with pytest.raises(SystemExit, match="--source beacon needs a value: --source beacon=<url>"):
        source_from_flag("beacon")
    with pytest.raises(SystemExit, match="--source local needs a value: --source local=<directory>"):
        source_from_flag("local=")


def test_R17_the_readers_sources_come_first_and_the_public_archives_follow():
    mine = [source_from_flag("beacon=http://localhost:5052"), source_from_flag("blockscout")]
    specs = recovery_sources(mine, 1, only_given=False)
    assert specs[:2] == mine
    # The archive the reader named is not asked a second time; the rest of the built-in list follows in its order.
    assert specs[2:] == [s for s in PUBLIC_SOURCES[1] if s != {"type": "blockscout"}] and len(specs) == len(PUBLIC_SOURCES[1]) + 1
    assert recovery_sources(mine, 1, only_given=True) == mine
    assert recovery_sources([], 11155111, only_given=False) == PUBLIC_SOURCES[11155111]


@pytest.mark.parametrize("chain_id", [1, 11155111])
def test_R17_the_built_in_public_archives_are_usable_sources_for_their_chain(chain_id):
    sources = build_sources(PUBLIC_SOURCES[chain_id], configuration.KNOWN_GENESIS[chain_id], configuration.SECONDS_PER_SLOT, chain_id)
    names = [s.name for s in sources]
    assert len(sources) == len(PUBLIC_SOURCES[chain_id]) >= 4 and len(set(names)) == len(names)
    # None of them needs a node of the reader's own, and each chain has an example RPC to show.
    assert not any(n.startswith(("ipfs", "local")) for n in names) and all("https://" in n for n in names)
    assert PUBLIC_RPC_EXAMPLE[chain_id].startswith("https://")


@pytest.mark.parametrize(
    "argv,message",
    [
        (["recover"], r"(?s)needs to know which dataset.*recover 0x… --rpc"),
        (["recover", ADDRESS], r"needs an Ethereum RPC endpoint.*--rpc <url>"),
        (["recover", "0x1234", "--rpc", "http://127.0.0.1:9"], r"'0x1234' is not an Ethereum address"),
        (["recover", ADDRESS[:-1] + "d", "--rpc", "http://127.0.0.1:9"], r"capital letters do not match its checksum"),
        (["recover", ADDRESS, "--rpc", "http://127.0.0.1:9", "--only-given-sources"], r"--only-given-sources.*needs at least one --source"),
        (["recover", ADDRESS, "--rpc", "http://127.0.0.1:9", "--source", "nope=http://x"], r"unknown source type 'nope'"),
        (["recover", ADDRESS, "--config", "c.toml"], r"either a config file or a contract address.*leave out the contract address"),
        (["recover", "--config", "c.toml", "--rpc", "http://x", "--from-block", "5"], r"with --config, leave out --rpc, --from-block"),
    ],
)
def test_R17_the_command_says_what_is_missing_or_mixed_up_before_touching_the_network(argv, message, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match=message):
        cli.main(argv)
    assert not (tmp_path / "recovery-data").exists() and not (tmp_path / "recovered").exists()


def test_R17_an_rpc_that_cannot_be_reached_is_said_plainly(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="the RPC endpoint given with --rpc could not be used"):
        cli.main(["recover", ADDRESS.lower(), "--rpc", "http://127.0.0.1:9"])


def test_H8_the_health_page_writes_out_the_recovery_command_for_its_dataset(tmp_path):
    from loculus_eternal.health_server import REPOSITORY, public_config

    def load(chain_lines: str):
        cfg = tmp_path / "loculus-eternal.toml"
        cfg.write_text(f'[chain]\nrpc_url = "https://rpc.example/v3/SECRET-KEY"\ncontract = "{ADDRESS}"\n{chain_lines}')
        return public_config(configuration.load(cfg))["recoverCommand"]

    clone, command = load("chain_id = 11155111\ndeployment_block = 11903469\n")
    assert clone == f"git clone {REPOSITORY} && cd loculus-eternal"
    assert command == f"uv run loculus-eternal recover {ADDRESS} --rpc {PUBLIC_RPC_EXAMPLE[11155111]} --from-block 11903469"
    # The operator's own RPC, which may carry a key, never appears; a public one for the chain does.
    assert "SECRET-KEY" not in command and "rpc.example" not in command
    assert load("chain_id = 1\n")[1] == f"uv run loculus-eternal recover {ADDRESS} --rpc {PUBLIC_RPC_EXAMPLE[1]}", "no starting block is claimed when none is configured"
    # A chain with no public example gets a placeholder the reader must fill in.
    assert load("chain_id = 31337\nbeacon_genesis_time = 0\n")[1].endswith("--rpc https://your-ethereum-rpc")
    page = (Path(cli.__file__).parent / "health_page.html").read_text()
    assert 'id="recover-command"' in page and 'id="copy-recover"' in page and "Recover this dataset yourself" in page
