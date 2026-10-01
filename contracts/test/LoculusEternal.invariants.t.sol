// SPDX-License-Identifier: AGPL-3.0-or-later
pragma solidity 0.8.30;

import {Test} from "forge-std/Test.sol";
import {LoculusEternal} from "../src/LoculusEternal.sol";

/// Drives the contract through random sequences of publishes, pointer updates, rotations
/// and rejected calls, mirroring every accepted blob off-chain so the invariants can be
/// checked against an independent record.
contract Handler is Test {
    LoculusEternal public record;
    address public publisher;
    bytes32[] public accepted;       // every versioned hash the contract accepted, in order
    bytes32 public mirrorHead;
    uint256 public rejected;

    constructor(LoculusEternal log_, address publisher_) {
        record = log_;
        publisher = publisher_;
    }

    function publish(uint8 count, uint64 wrongSeq, bool useWrongSeq, uint32 chunks, bool batchEnd, bytes32 pointer) external {
        uint256 n = bound(count, 0, 7);
        bytes32[] memory hs = new bytes32[](n);
        for (uint256 i = 0; i < n; i++) {
            hs[i] = bytes32((uint256(0x01) << 248) | (uint256(keccak256(abi.encodePacked(accepted.length, i, chunks))) >> 8));
        }
        vm.blobhashes(hs);
        uint64 expected = useWrongSeq ? wrongSeq : record.blobCount();
        vm.prank(publisher);
        try record.publish(expected, chunks, batchEnd, pointer) {
            // The contract accepted: it must have had blobs, a valid chunk count and the
            // right sequence number. Mirror the append.
            require(n > 0 && chunks >= 1 && chunks <= 4096 && expected == uint64(accepted.length), "accepted an invalid call");
            for (uint256 i = 0; i < n; i++) {
                mirrorHead = keccak256(abi.encodePacked(mirrorHead, hs[i]));
                accepted.push(hs[i]);
            }
        } catch {
            rejected++;
        }
    }

    function rotate(address next) external {
        if (next == address(0)) return;
        vm.prank(publisher);
        record.setPublisher(next);
        publisher = next;
    }

    function strangerTries(address who, uint32 chunks) external {
        vm.assume(who != publisher);
        bytes32[] memory hs = new bytes32[](1);
        hs[0] = bytes32(uint256(0x01) << 248 | uint256(1));
        vm.blobhashes(hs);
        vm.prank(who);
        try record.publish(record.blobCount(), chunks, true, bytes32(0)) {
            revert("a stranger published");
        } catch {
            rejected++;
        }
    }

    function acceptedCount() external view returns (uint256) {
        return accepted.length;
    }
}

contract LoculusEternalInvariants is Test {
    LoculusEternal internal record;
    Handler internal handler;

    function setUp() public {
        address publisher = address(0xA11CE);
        record = new LoculusEternal(publisher);
        handler = new Handler(record, publisher);
        targetContract(address(handler));
    }

    /// C12: head is exactly the hash chain over the accepted blobs, and blobCount counts them.
    function invariant_C12_head_is_chain_over_accepted_blobs() public view {
        assertEq(record.head(), handler.mirrorHead());
        assertEq(record.blobCount(), handler.acceptedCount());
    }

    /// C6 as an invariant: only the current publisher ever appended.
    function invariant_C6_publisher_is_the_handler_publisher() public view {
        assertEq(record.publisher(), handler.publisher());
    }
}
