// SPDX-License-Identifier: AGPL-3.0-or-later
pragma solidity 0.8.30;

/// @title LoculusEternal
/// @notice The permanent record of every blob Loculus Eternal has published for Pathoplexus.
///
/// The contract is deliberately tiny and immutable: no upgrade path, no admin, no pause.
/// It is the correctness anchor for everything else. A publisher sends blob-carrying
/// transactions to `publish`; the contract reads each attached blob's versioned hash straight
/// from the transaction, folds it into a hash chain, and emits one event per blob. Anyone who
/// later obtains a list of versioned hashes from anywhere can recompute the chain and compare
/// it with `head`, so the record survives even if old event logs stop being served.
///
/// The publisher is trusted only for what to append. It cannot alter or remove anything, and
/// it cannot forge a versioned hash because the EVM supplies them.
contract LoculusEternal {
    /// @notice The only address allowed to append. Rotatable by itself, see `setPublisher`.
    address public publisher;

    /// @notice Number of blobs ever published. Also the sequence number of the next blob.
    uint64 public blobCount;

    /// @notice The hash chain over every versioned hash in order: starts at zero and becomes
    /// keccak256(head ‖ versionedHash) for each blob appended.
    bytes32 public head;

    /// @notice The publisher's claim of where the current IPFS snapshot lives, as the
    /// SHA-256 of the snapshot CID's bytes. Never interpreted by the contract.
    bytes32 public appPointer;

    /// @notice A write-once breadcrumb to a replacement deployment, if one is ever needed.
    address public successor;

    /// @notice One per blob, in order. `seq` is the blob's position in the stream.
    event BlobPublished(uint64 indexed seq, bytes32 versionedHash);

    /// @notice Emitted by the transaction that completes a batch. `lastBlobChunkCount` says
    /// how many 31-byte chunks of blob `lastSeq` carry data.
    event BatchCommitted(uint64 firstSeq, uint64 lastSeq, uint32 lastBlobChunkCount, bytes32 appPointer);

    event AppPointerSet(bytes32 appPointer);
    event SuccessorSet(address successor);
    event PublisherChanged(address indexed previous, address indexed current);

    error NotPublisher();
    error ZeroAddress();
    error NoBlobs();
    error BadChunkCount(uint32 count);
    error SequenceMismatch(uint64 expected, uint64 actual);
    error SuccessorAlreadySet();

    uint32 internal constant CHUNKS_PER_BLOB = 4096;

    constructor(address publisher_) {
        if (publisher_ == address(0)) revert ZeroAddress();
        publisher = publisher_;
        emit PublisherChanged(address(0), publisher_);
    }

    modifier onlyPublisher() {
        if (msg.sender != publisher) revert NotPublisher();
        _;
    }

    /// @notice Append every blob attached to this transaction to the record.
    /// @param expectedFirstSeq The sequence number the caller expects the first new blob to
    ///        get. The call reverts if the record has moved on, so two uploads running at once,
    ///        or a stale resubmission, cannot interleave blobs into the stream.
    /// @param lastBlobChunkCount How many 31-byte chunks of the final attached blob carry data,
    ///        1 to 4096. Blobs that are not the last of their batch are always full (4096).
    /// @param isBatchEnd True when this transaction completes a batch; the stream is then
    ///        coherent up to and including these blobs.
    /// @param newAppPointer The new snapshot pointer, applied only when `isBatchEnd` is true.
    function publish(uint64 expectedFirstSeq, uint32 lastBlobChunkCount, bool isBatchEnd, bytes32 newAppPointer)
        external
        onlyPublisher
    {
        if (lastBlobChunkCount == 0 || lastBlobChunkCount > CHUNKS_PER_BLOB) revert BadChunkCount(lastBlobChunkCount);
        uint64 first = blobCount;
        if (first != expectedFirstSeq) revert SequenceMismatch(first, expectedFirstSeq);

        bytes32 chain = head;
        uint64 seq = first;
        // BLOBHASH returns zero past the last attached blob, which is how the loop ends.
        for (uint256 i = 0;; i++) {
            bytes32 versionedHash = blobhash(i);
            if (versionedHash == bytes32(0)) break;
            chain = keccak256(abi.encodePacked(chain, versionedHash));
            emit BlobPublished(seq, versionedHash);
            seq++;
        }
        if (seq == first) revert NoBlobs();

        head = chain;
        blobCount = seq;
        if (isBatchEnd) {
            appPointer = newAppPointer;
            emit BatchCommitted(first, seq - 1, lastBlobChunkCount, newAppPointer);
        }
    }

    /// @notice Re-point the snapshot claim without publishing anything.
    function setAppPointer(bytes32 newAppPointer) external onlyPublisher {
        appPointer = newAppPointer;
        emit AppPointerSet(newAppPointer);
    }

    /// @notice Leave the breadcrumb to a replacement deployment. Can be done once, ever.
    function setSuccessor(address newSuccessor) external onlyPublisher {
        if (newSuccessor == address(0)) revert ZeroAddress();
        if (successor != address(0)) revert SuccessorAlreadySet();
        successor = newSuccessor;
        emit SuccessorSet(newSuccessor);
    }

    /// @notice Hand the publisher role to another key. Covers planned handover and rotating a
    /// key before it is lost. It does not help after a compromise: whoever holds the key can
    /// rotate it away.
    function setPublisher(address newPublisher) external onlyPublisher {
        if (newPublisher == address(0)) revert ZeroAddress();
        emit PublisherChanged(publisher, newPublisher);
        publisher = newPublisher;
    }
}
