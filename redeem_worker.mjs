import 'dotenv/config';

import {
  concat,
  createPublicClient,
  encodeFunctionData,
  getAddress,
  http,
  keccak256,
  toBytes,
  toHex,
  zeroHash,
} from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { polygon } from 'viem/chains';

const DATA_API = 'https://data-api.polymarket.com';
const RELAYER_API = 'https://relayer-v2.polymarket.com';

const CTF_ADDRESS = '0x4d97dcd97ec945f40cf65f87097ace5ea0476045';
const USDC_ADDRESS = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174';
const PROXY_FACTORY_ADDRESS = '0xaB45c5A4B0c941a2F231C04C3f49182e1A254052';
const RELAY_HUB_ADDRESS = '0xD216153c06E857cD7f72665E0aF1d7D82172F494';
const DEFAULT_PROXY_GAS_LIMIT = 10_000_000n;
const POLL_INTERVAL_MS = 3000;
const MAX_POLL_ATTEMPTS = 20;

const proxyWalletFactoryAbi = [
  {
    inputs: [
      {
        components: [
          { name: 'typeCode', type: 'uint8' },
          { name: 'to', type: 'address' },
          { name: 'value', type: 'uint256' },
          { name: 'data', type: 'bytes' },
        ],
        name: 'calls',
        type: 'tuple[]',
      },
    ],
    name: 'proxy',
    outputs: [{ name: 'returnValues', type: 'bytes[]' }],
    stateMutability: 'payable',
    type: 'function',
  },
];

const ctfRedeemAbi = [
  {
    inputs: [
      { name: 'collateralToken', type: 'address' },
      { name: 'parentCollectionId', type: 'bytes32' },
      { name: 'conditionId', type: 'bytes32' },
      { name: 'indexSets', type: 'uint256[]' },
    ],
    name: 'redeemPositions',
    outputs: [],
    stateMutability: 'nonpayable',
    type: 'function',
  },
];

function getEnv(name, fallback = '') {
  return process.env[name] || fallback;
}

function parseArgs(argv) {
  let maxItems = null;
  let conditionId = '';

  for (const arg of argv) {
    if (arg.startsWith('--max-items=')) {
      const raw = arg.slice('--max-items='.length).trim();
      if (!raw) {
        throw new Error('Expected value for --max-items');
      }
      const parsed = Number.parseInt(raw, 10);
      if (!Number.isFinite(parsed) || parsed <= 0) {
        throw new Error(`Invalid --max-items value: ${raw}`);
      }
      maxItems = parsed;
    } else if (arg.startsWith('--condition-id=')) {
      conditionId = arg.slice('--condition-id='.length).trim();
      if (!conditionId) {
        throw new Error('Expected value for --condition-id');
      }
    }
  }

  return {
    dryRun: argv.includes('--dry-run'),
    maxItems,
    conditionId,
  };
}

function requiredEnv(name) {
  const value = getEnv(name);
  if (!value) {
    throw new Error(`Missing required env: ${name}`);
  }
  return value;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function toAddress(value, name) {
  try {
    return getAddress(value);
  } catch {
    throw new Error(`Invalid ${name}: ${value}`);
  }
}

function encodeProxyTransactionData(transactions) {
  return encodeFunctionData({
    abi: proxyWalletFactoryAbi,
    functionName: 'proxy',
    args: [transactions],
  });
}

function buildCtfRedeemCall(conditionId) {
  return {
    to: CTF_ADDRESS,
    typeCode: 1,
    value: 0n,
    data: encodeFunctionData({
      abi: ctfRedeemAbi,
      functionName: 'redeemPositions',
      args: [USDC_ADDRESS, zeroHash, conditionId, [1n, 2n]],
    }),
  };
}

function createProxyStructHash({ from, to, data, txFee, gasPrice, gasLimit, nonce, relayHub, relay }) {
  const relayHubPrefix = toHex('rlx:');

  return keccak256(
    concat([
      relayHubPrefix,
      from,
      to,
      data,
      toHex(BigInt(txFee), { size: 32 }),
      toHex(BigInt(gasPrice), { size: 32 }),
      toHex(BigInt(gasLimit), { size: 32 }),
      toHex(BigInt(nonce), { size: 32 }),
      relayHub,
      relay,
    ])
  );
}

async function createProxySignature(account, structHash) {
  return account.signMessage({ message: { raw: toBytes(structHash) } });
}

function createPublicPolygonClient() {
  const rpcUrl = getEnv('POLYGON_RPC_URL');
  if (!rpcUrl) {
    return null;
  }

  return createPublicClient({
    chain: polygon,
    transport: http(rpcUrl),
  });
}

async function estimateProxyGasLimit(publicClient, from, data) {
  if (!publicClient) {
    return DEFAULT_PROXY_GAS_LIMIT.toString();
  }

  try {
    const gas = await publicClient.estimateGas({
      account: from,
      to: PROXY_FACTORY_ADDRESS,
      data,
      value: 0n,
    });
    return gas.toString();
  } catch (error) {
    console.warn(`Gas estimate failed, using default ${DEFAULT_PROXY_GAS_LIMIT}: ${error.message}`);
    return DEFAULT_PROXY_GAS_LIMIT.toString();
  }
}

async function fetchRelayerJson(path, options = {}) {
  const response = await fetch(`${RELAYER_API}${path}`, options);
  if (!response.ok) {
    const body = await response.text().catch(() => '');
    throw new Error(`relayer request failed: ${response.status} ${response.statusText}${body ? ` | ${body}` : ''}`);
  }
  return response.json();
}

async function fetchProxyRelayPayload(ownerAddress) {
  const url = new URL(`${RELAYER_API}/relay-payload`);
  url.searchParams.set('address', ownerAddress);
  url.searchParams.set('type', 'PROXY');
  return fetchRelayerJson(url.pathname + url.search);
}

async function fetchRelayerTransaction(transactionId) {
  const url = new URL(`${RELAYER_API}/transaction`);
  url.searchParams.set('id', transactionId);
  const payload = await fetchRelayerJson(url.pathname + url.search);
  return Array.isArray(payload) ? payload[0] || null : payload;
}

async function submitProxyTransaction(request, relayerApiKey, relayerApiKeyAddress) {
  return fetchRelayerJson('/submit', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'RELAYER_API_KEY': relayerApiKey,
      'RELAYER_API_KEY_ADDRESS': relayerApiKeyAddress,
    },
    body: JSON.stringify(request),
  });
}

async function waitForRelayerTransaction(transactionId) {
  for (let attempt = 1; attempt <= MAX_POLL_ATTEMPTS; attempt += 1) {
    const txn = await fetchRelayerTransaction(transactionId);
    if (!txn) {
      await sleep(POLL_INTERVAL_MS);
      continue;
    }

    console.log(`  poll ${attempt}/${MAX_POLL_ATTEMPTS}: state=${txn.state}${txn.transactionHash ? ` hash=${txn.transactionHash}` : ''}`);

    if (txn.state === 'STATE_CONFIRMED' || txn.state === 'STATE_MINED' || txn.state === 'STATE_EXECUTED') {
      return txn;
    }

    if (txn.state === 'STATE_FAILED' || txn.state === 'STATE_INVALID') {
      throw new Error(`relayer transaction ${transactionId} ended in ${txn.state}`);
    }

    await sleep(POLL_INTERVAL_MS);
  }

  throw new Error(`Timed out waiting for relayer transaction ${transactionId}`);
}

async function buildProxyRedeemRequest({ account, proxyWallet, publicClient, ctfPositions, metadata }) {
  const from = toAddress(account.address, 'signer address');
  const relayPayload = await fetchProxyRelayPayload(from);
  const relayAddress = toAddress(relayPayload.address, 'relayer address');
  const nonce = String(relayPayload.nonce);

  const calls = ctfPositions.map((pos) => buildCtfRedeemCall(pos.conditionId));
  const data = encodeProxyTransactionData(calls);
  const gasPrice = '0';
  const relayerFee = '0';
  const gasLimit = await estimateProxyGasLimit(publicClient, from, data);

  const signature = await createProxySignature(
    account,
    createProxyStructHash({
      from,
      to: PROXY_FACTORY_ADDRESS,
      data,
      txFee: relayerFee,
      gasPrice,
      gasLimit,
      nonce,
      relayHub: RELAY_HUB_ADDRESS,
      relay: relayAddress,
    })
  );

  return {
    from,
    to: PROXY_FACTORY_ADDRESS,
    proxyWallet,
    data,
    nonce,
    signature,
    signatureParams: {
      gasPrice,
      gasLimit,
      relayerFee,
      relayHub: RELAY_HUB_ADDRESS,
      relay: relayAddress,
    },
    type: 'PROXY',
    metadata: metadata || '',
  };
}

async function executeLiveCtfRedeem({ privateKey, proxyWallet, relayerApiKey, relayerApiKeyAddress, ctfPositions }) {
  const account = privateKeyToAccount(privateKey);
  const signerAddress = toAddress(account.address, 'POLY_PRIVATE_KEY address');
  const relayerOwnerAddress = toAddress(relayerApiKeyAddress, 'RELAYER_API_KEY_ADDRESS');
  const publicClient = createPublicPolygonClient();

  const metadata = `redeem ${ctfPositions.length} CTF position(s)`;
  const request = await buildProxyRedeemRequest({
    account,
    proxyWallet,
    publicClient,
    ctfPositions,
    metadata,
  });

  console.log(`Submitting PROXY redeem from signer ${signerAddress} for proxy ${request.proxyWallet}`);
  console.log(`Relayer auth owner: ${relayerOwnerAddress}`);
  console.log(`Batch contains ${ctfPositions.length} condition(s)`);

  const submitResult = await submitProxyTransaction(request, relayerApiKey, relayerOwnerAddress);
  console.log(`Relayer accepted transaction: id=${submitResult.transactionID} state=${submitResult.state}`);

  const finalTxn = await waitForRelayerTransaction(submitResult.transactionID);
  console.log(`Redeem confirmed: state=${finalTxn.state} hash=${finalTxn.transactionHash || 'pending-hash-unavailable'}`);
  return finalTxn;
}

async function fetchRedeemablePositions(user) {
  const url = new URL(`${DATA_API}/positions`);
  url.searchParams.set('user', user);
  url.searchParams.set('redeemable', 'true');
  url.searchParams.set('sizeThreshold', '0');

  const response = await fetch(url, {
    headers: {
      'accept': 'application/json',
      'user-agent': '5m-poly-bot-redeem-worker/1.0'
    }
  });

  if (!response.ok) {
    throw new Error(`positions request failed: ${response.status} ${response.statusText}`);
  }

  const payload = await response.json();
  return Array.isArray(payload) ? payload : [];
}

function selectWinningRedeems(positions) {
  return positions
    .filter((pos) => pos && pos.redeemable === true)
    .map((pos) => ({
      title: pos.title || pos.slug || pos.conditionId,
      conditionId: pos.conditionId,
      outcome: pos.outcome,
      outcomeIndex: pos.outcomeIndex,
      size: Number(pos.size || 0),
      currentValue: Number(pos.currentValue || 0),
      negativeRisk: Boolean(pos.negativeRisk),
      raw: pos,
    }))
    .filter((pos) => pos.currentValue > 0 && pos.size > 0 && !!pos.conditionId);
}

function filterRedeemTargets(positions, args) {
  let filtered = [...positions];

  if (args.conditionId) {
    const wanted = args.conditionId.toLowerCase();
    filtered = filtered.filter((pos) => String(pos.conditionId).toLowerCase() === wanted);
  }

  if (args.maxItems != null) {
    filtered = filtered.slice(0, args.maxItems);
  }

  return filtered;
}

async function executeWithFallback(params) {
  const { ctfPositions } = params;

  if (ctfPositions.length === 1) {
    return [await executeLiveCtfRedeem(params)];
  }

  try {
    return [await executeLiveCtfRedeem(params)];
  } catch (error) {
    console.warn(`Batch redeem failed, falling back to one-by-one mode: ${error.message}`);
  }

  const results = [];
  for (const pos of ctfPositions) {
    console.log(`\nRetrying single redeem for conditionId=${pos.conditionId} | title=${pos.title}`);
    const result = await executeLiveCtfRedeem({
      ...params,
      ctfPositions: [pos],
    });
    results.push(result);
  }

  return results;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const proxyWallet = toAddress(requiredEnv('POLY_PROXY_WALLET'), 'POLY_PROXY_WALLET');

  const relayerApiKey = getEnv('RELAYER_API_KEY');
  const relayerApiKeyAddress = getEnv('RELAYER_API_KEY_ADDRESS');
  const privateKey = getEnv('POLY_PRIVATE_KEY');

  console.log('=== Redeem Worker ===');
  console.log(`Proxy wallet: ${proxyWallet}`);
  console.log(`Mode: ${args.dryRun ? 'DRY-RUN' : 'LIVE'}`);
  console.log(`Max items: ${args.maxItems == null ? 'ALL' : args.maxItems}`);
  console.log(`Condition filter: ${args.conditionId || 'NONE'}`);
  console.log(`Relayer API key: ${relayerApiKey ? 'SET' : 'MISSING'}`);
  console.log(`Relayer API key address: ${relayerApiKeyAddress ? 'SET' : 'MISSING'}`);
  console.log(`Private key: ${privateKey ? 'SET' : 'MISSING'}`);
  console.log(`Polygon RPC: ${getEnv('POLYGON_RPC_URL') ? 'SET' : 'NOT SET (default gas limit fallback)'}`);

  const positions = await fetchRedeemablePositions(proxyWallet);
  const winningRedeems = selectWinningRedeems(positions);

  if (!winningRedeems.length) {
    console.log('No redeemable winning positions found.');
    return;
  }

  console.log(`Found ${winningRedeems.length} redeemable winning position(s):`);
  for (const pos of winningRedeems) {
    console.log(
      `- ${pos.title} | outcome=${pos.outcome} | value=$${pos.currentValue.toFixed(4)} | size=${pos.size.toFixed(4)} | negativeRisk=${pos.negativeRisk}`
    );
  }

  const negRiskPositions = winningRedeems.filter((pos) => pos.negativeRisk);
  const ctfPositions = filterRedeemTargets(
    winningRedeems.filter((pos) => !pos.negativeRisk),
    args
  );

  if (ctfPositions.length) {
    console.log(`\nCTF redeem candidates: ${ctfPositions.length}`);
    for (const pos of ctfPositions) {
      console.log(`  conditionId=${pos.conditionId} | title=${pos.title}`);
    }
  } else if (winningRedeems.some((pos) => !pos.negativeRisk) && (args.conditionId || args.maxItems != null)) {
    console.log('\nNo CTF redeem candidates left after applying filters.');
  }

  if (negRiskPositions.length) {
    console.log(`\nNegRisk redeem candidates: ${negRiskPositions.length}`);
    console.log('NegRisk execution is intentionally not implemented in this first worker version.');
  }

  if (args.dryRun) {
    console.log('\nDry-run only. No transactions submitted.');
    return;
  }

  if (!ctfPositions.length) {
    console.log('\nNo non-NegRisk CTF positions to redeem live. Nothing submitted.');
    return;
  }

  if (negRiskPositions.length) {
    console.log('\nSkipping NegRisk positions in live mode; only standard CTF redemption is implemented.');
  }

  requiredEnv('POLY_PRIVATE_KEY');
  requiredEnv('RELAYER_API_KEY');
  requiredEnv('RELAYER_API_KEY_ADDRESS');

  const results = await executeWithFallback({
    privateKey,
    proxyWallet,
    relayerApiKey,
    relayerApiKeyAddress,
    ctfPositions,
  });

  console.log(`\nCompleted redeem submission(s): ${results.length}`);
}

main().catch((error) => {
  console.error(`Redeem worker failed: ${error.message}`);
  process.exitCode = 1;
});
