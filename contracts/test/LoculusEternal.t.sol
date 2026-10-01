// SPDX-License-Identifier: AGPL-3.0-or-later
pragma solidity 0.8.30;

import {Test} from "forge-std/Test.sol";
import {LoculusEternal} from "../src/LoculusEternal.sol";

/// Unit tests for behaviours C1 to C11. The BLOBHASH opcode is driven with the
/// `vm.blobhashes` cheatcode; real blob transactions are exercised by the Python harness.
contract LoculusEternalTest is Test {
    LoculusEternal internal record;
    address internal publisher = address(0xA11CE);
    address internal stranger = address(0xB0B);

    event BlobPublished(uint64 indexed seq, bytes32 versionedHash);
    event BatchCommitted(uint64 firstSeq, uint64 lastSeq, uint32 lastBlobChunkCount, bytes32 appPointer);
    event AppPointerSet(bytes32 appPointer);
    event SuccessorSet(address successor);
    event PublisherChanged(address indexed previous, address indexed current);

    function setUp() public {
        record = new LoculusEternal(publisher);
    }

    /// A versioned hash as Ethereum forms it: version byte 0x01 then 31 bytes of digest.
    function vh(uint256 n) internal pure returns (bytes32) {
        return bytes32((uint256(0x01) << 248) | (uint256(keccak256(abi.encodePacked("blob", n))) >> 8));
    }

    function hashes(uint256 count, uint256 offset) internal pure returns (bytes32[] memory out) {
        out = new bytes32[](count);
        for (uint256 i = 0; i < count; i++) {
            out[i] = vh(offset + i);
        }
    }

    function chain(bytes32 start, bytes32[] memory hs) internal pure returns (bytes32 h) {
        h = start;
        for (uint256 i = 0; i < hs.length; i++) {
            h = keccak256(abi.encodePacked(h, hs[i]));
        }
    }

    // --- C1 append order and per-blob events --------------------------------------------

    function test_C1_publish_appends_in_order_and_emits_one_event_per_blob() public {
        bytes32[] memory hs = hashes(3, 0);
        vm.blobhashes(hs);
        vm.prank(publisher);
        for (uint64 i = 0; i < 3; i++) {
            vm.expectEmit(true, false, false, true);
            emit BlobPublished(i, hs[i]);
        }
        record.publish(0, 4096, false, bytes32(0));
        assertEq(record.blobCount(), 3);

        bytes32[] memory more = hashes(2, 3);
        vm.blobhashes(more);
        vm.prank(publisher);
        vm.expectEmit(true, false, false, true);
        emit BlobPublished(3, more[0]);
        vm.expectEmit(true, false, false, true);
        emit BlobPublished(4, more[1]);
        record.publish(3, 100, true, bytes32(uint256(7)));
        assertEq(record.blobCount(), 5);
    }

    // --- C2 head is the chain over all hashes -----------------------------------------------

    function test_C2_head_is_the_keccak_chain_over_all_published_hashes() public {
        bytes32[] memory a = hashes(4, 0);
        vm.blobhashes(a);
        vm.prank(publisher);
        record.publish(0, 4096, false, bytes32(0));
        assertEq(record.head(), chain(bytes32(0), a));

        bytes32[] memory b = hashes(2, 4);
        vm.blobhashes(b);
        vm.prank(publisher);
        record.publish(4, 1, true, bytes32(0));
        assertEq(record.head(), chain(chain(bytes32(0), a), b));
    }

    function testFuzz_C2_head_matches_recomputed_chain(uint8 n1, uint8 n2) public {
        uint256 c1 = bound(n1, 1, 6);
        uint256 c2 = bound(n2, 1, 6);
        bytes32[] memory a = hashes(c1, 0);
        bytes32[] memory b = hashes(c2, c1);
        vm.blobhashes(a);
        vm.prank(publisher);
        record.publish(0, 4096, false, bytes32(0));
        vm.blobhashes(b);
        vm.prank(publisher);
        record.publish(uint64(c1), 4096, true, bytes32(0));
        assertEq(record.head(), chain(chain(bytes32(0), a), b));
        assertEq(record.blobCount(), c1 + c2);
    }

    // --- C3 zero blobs ----------------------------------------------------------------------

    function test_C3_publish_with_no_blobs_reverts() public {
        vm.blobhashes(new bytes32[](0));
        vm.prank(publisher);
        vm.expectRevert(LoculusEternal.NoBlobs.selector);
        record.publish(0, 4096, false, bytes32(0));
        assertEq(record.blobCount(), 0);
        assertEq(record.head(), bytes32(0));
    }

    // --- C4 chunk count bounds --------------------------------------------------------------

    function test_C4_chunk_count_zero_and_above_4096_revert() public {
        vm.blobhashes(hashes(1, 0));
        vm.prank(publisher);
        vm.expectRevert(abi.encodeWithSelector(LoculusEternal.BadChunkCount.selector, uint32(0)));
        record.publish(0, 0, true, bytes32(0));
        vm.prank(publisher);
        vm.expectRevert(abi.encodeWithSelector(LoculusEternal.BadChunkCount.selector, uint32(4097)));
        record.publish(0, 4097, true, bytes32(0));
    }

    function test_C4_chunk_count_1_and_4096_are_accepted() public {
        vm.blobhashes(hashes(1, 0));
        vm.prank(publisher);
        record.publish(0, 1, true, bytes32(0));
        vm.blobhashes(hashes(1, 1));
        vm.prank(publisher);
        record.publish(1, 4096, true, bytes32(0));
        assertEq(record.blobCount(), 2);
    }

    // --- C5 sequence guard ------------------------------------------------------------------

    function test_C5_wrong_expected_sequence_reverts_and_changes_nothing() public {
        vm.blobhashes(hashes(2, 0));
        vm.prank(publisher);
        record.publish(0, 4096, true, bytes32(0));
        vm.blobhashes(hashes(1, 2));
        vm.prank(publisher);
        vm.expectRevert(abi.encodeWithSelector(LoculusEternal.SequenceMismatch.selector, uint64(2), uint64(1)));
        record.publish(1, 4096, true, bytes32(0));
        vm.prank(publisher);
        vm.expectRevert(abi.encodeWithSelector(LoculusEternal.SequenceMismatch.selector, uint64(2), uint64(3)));
        record.publish(3, 4096, true, bytes32(0));
        assertEq(record.blobCount(), 2);
    }

    // --- C6 authorization -------------------------------------------------------------------

    function test_C6_only_the_publisher_can_call_anything_that_writes() public {
        vm.blobhashes(hashes(1, 0));
        vm.startPrank(stranger);
        vm.expectRevert(LoculusEternal.NotPublisher.selector);
        record.publish(0, 4096, true, bytes32(0));
        vm.expectRevert(LoculusEternal.NotPublisher.selector);
        record.setAppPointer(bytes32(uint256(1)));
        vm.expectRevert(LoculusEternal.NotPublisher.selector);
        record.setSuccessor(stranger);
        vm.expectRevert(LoculusEternal.NotPublisher.selector);
        record.setPublisher(stranger);
        vm.stopPrank();
        assertEq(record.blobCount(), 0);
        assertEq(record.publisher(), publisher);
    }

    // --- C7 write-once successor ------------------------------------------------------------

    function test_C7_successor_is_write_once_and_nonzero() public {
        vm.startPrank(publisher);
        vm.expectRevert(LoculusEternal.ZeroAddress.selector);
        record.setSuccessor(address(0));
        vm.expectEmit(false, false, false, true);
        emit SuccessorSet(address(0xCAFE));
        record.setSuccessor(address(0xCAFE));
        vm.expectRevert(LoculusEternal.SuccessorAlreadySet.selector);
        record.setSuccessor(address(0xBEEF));
        vm.stopPrank();
        assertEq(record.successor(), address(0xCAFE));
    }

    // --- C8 batch end -----------------------------------------------------------------------

    function test_C8_batch_end_emits_commit_and_sets_pointer_while_mid_batch_does_not() public {
        vm.blobhashes(hashes(6, 0));
        vm.prank(publisher);
        record.publish(0, 4096, false, bytes32(uint256(0xBAD)));
        assertEq(record.appPointer(), bytes32(0), "mid-batch call must not touch the pointer");

        vm.blobhashes(hashes(3, 6));
        vm.prank(publisher);
        vm.expectEmit(false, false, false, true);
        emit BatchCommitted(6, 8, 1234, bytes32(uint256(0xF00D)));
        record.publish(6, 1234, true, bytes32(uint256(0xF00D)));
        assertEq(record.appPointer(), bytes32(uint256(0xF00D)));
    }

    // --- C9 publisher rotation --------------------------------------------------------------

    function test_C9_set_publisher_rotates_the_role_and_rejects_zero() public {
        vm.prank(publisher);
        vm.expectRevert(LoculusEternal.ZeroAddress.selector);
        record.setPublisher(address(0));

        vm.prank(publisher);
        vm.expectEmit(true, true, false, true);
        emit PublisherChanged(publisher, stranger);
        record.setPublisher(stranger);
        assertEq(record.publisher(), stranger);

        vm.blobhashes(hashes(1, 0));
        vm.prank(publisher);
        vm.expectRevert(LoculusEternal.NotPublisher.selector);
        record.publish(0, 4096, true, bytes32(0));
        vm.prank(stranger);
        record.publish(0, 4096, true, bytes32(0));
        assertEq(record.blobCount(), 1);
    }

    // --- C10 standalone re-point ------------------------------------------------------------

    function test_C10_set_app_pointer_standalone() public {
        vm.prank(publisher);
        vm.expectEmit(false, false, false, true);
        emit AppPointerSet(bytes32(uint256(42)));
        record.setAppPointer(bytes32(uint256(42)));
        assertEq(record.appPointer(), bytes32(uint256(42)));
        assertEq(record.blobCount(), 0);
    }

    // --- C11 constructor --------------------------------------------------------------------

    function test_C11_constructor_rejects_zero_publisher_and_announces_the_first() public {
        vm.expectRevert(LoculusEternal.ZeroAddress.selector);
        new LoculusEternal(address(0));
        vm.expectEmit(true, true, false, true);
        emit PublisherChanged(address(0), publisher);
        LoculusEternal fresh = new LoculusEternal(publisher);
        assertEq(fresh.publisher(), publisher);
        assertEq(fresh.blobCount(), 0);
        assertEq(fresh.head(), bytes32(0));
    }

    // --- C13 size and gas, recorded in docs/contract.md ----------------------------------------

    function test_C13_runtime_code_is_small() public view {
        assertLt(address(record).code.length, 4096, "contract should stay tiny");
    }
}
