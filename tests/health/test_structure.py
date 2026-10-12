"""The structure walker's view of a stream with an abandoned first attempt."""

from loculus_eternal.format.gen_vectors import sample_entry
from loculus_eternal.format.records import CODEC_RAW
from loculus_eternal.format.encode import StreamEncoder
from loculus_eternal.format.structure import read_structure


def test_H2_structure_names_the_dead_blobs_and_reads_the_same_without_them():
    chain_id, contract = 11155111, bytes(20)
    big = [sample_entry("zika", f"PP_00070{i}", 1, seq_len=60000) for i in range(3)]
    # A genesis attempt whose batch-end transaction never landed, then genesis started again.
    torn_blobs = StreamEncoder(chain_id, contract).encode_batch(big, codec=CODEC_RAW).blobs[:-1]
    retry_enc = StreamEncoder(chain_id, contract)
    retry_enc.state.next_blob_seq = len(torn_blobs)
    retry = retry_enc.encode_batch(big, codec=CODEC_RAW)
    blobs = torn_blobs + retry.blobs
    full = read_structure(blobs)
    assert [b.batch for b in full.batches] == [0] and full.batches[0].first_blob_seq == len(torn_blobs)
    assert full.dead_blobs() == set(range(1, len(torn_blobs))) and full.torn_tail() is None
    without = [b if i == 0 or i >= len(torn_blobs) else None for i, b in enumerate(blobs)]
    partial = read_structure(without)
    assert partial.header is not None and [b.manifest_digest for b in partial.batches] == [b.manifest_digest for b in full.batches]
    assert partial.dead_blobs() == full.dead_blobs()
    # A torn tail is not dead.
    tail = read_structure(blobs + retry.blobs[:1])
    assert tail.torn_tail() is not None and tail.dead_blobs() == full.dead_blobs()
