// SPDX-License-Identifier: AGPL-3.0-or-later
pragma solidity 0.8.30;

import {Script, console} from "forge-std/Script.sol";
import {LoculusEternal} from "../src/LoculusEternal.sol";

/// Deploys LoculusEternal with the publisher taken from the PUBLISHER environment variable.
///
///   PUBLISHER=0x... forge script script/Deploy.s.sol --rpc-url $RPC --broadcast --private-key $DEPLOYER_KEY
///
/// The deployer and the publisher may be different keys; the deployer has no role afterwards.
contract Deploy is Script {
    function run() external returns (LoculusEternal deployed) {
        address publisher = vm.envAddress("PUBLISHER");
        vm.startBroadcast();
        deployed = new LoculusEternal(publisher);
        vm.stopBroadcast();
        console.log("LoculusEternal deployed at", address(deployed));
        console.log("publisher", publisher);
    }
}
