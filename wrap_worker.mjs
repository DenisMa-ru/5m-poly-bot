import 'dotenv/config';

import {
  concat,
  createPublicClient,
  encodeAbiParameters,
  encodeFunctionData,
  formatUnits,
  getAddress,
  getCreate2Address,
  hashTypedData,
  http,
  keccak256,
  toBytes,
  toHex,
  zeroAddress,
} from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { polygon } from 'viem/chains';

const RELAYER_API = 'https://relayer-v2.polymarket.com';

const USDCE_ADDRESS = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174';
const PUSD_ONRAMP_ADDRESS = '0x93070a847efEf7F70739046A929D47a521F5B8ee';
const PROXY_FACTORY_ADDRESS = '0xaB45c5A4B0c941a2F231C04C3f49182e1A254052';
const RELAY_HUB_ADDRESS = '0xD216153c06E857cD7f72665E0aF1d7D82172F494';
const SAFE_FACTORY_ADDRESS = '0xaacfeea03eb1561c4e67d661e40682bd20e3541b';
const SAFE_INIT_CODE_HASH = '0x2bce2127ff07fb632d16c8347c4ebf501f4841168bed00d9e6ef715ddb6fcecf';
const DEFAULT_PROXY_GAS_LIMIT = 10_000_000n;
const POLL_INTERVAL_MS = 3000;
const MAX_POLL_ATTEMPTS = 40;
const USDCE_DECIMALS = 6;
const DEFAULT_MIN_WRAP = 0.01;
const MAX_UINT256 = (1n << 256n) - 1n;

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

const erc20Abi = [
  {
    name: 'balanceOf',
    type: 'function',
    stateMutability: 'view',
    inputs: [{ name: 'account', type: 'address' }],
    outputs: [{ name: '', type: 'uint256' }],
  },
  {
    name: 'allowance',
    type: 'function',
    stateMutability: 'view',
    inputs: [
      { name: 'owner', type: 'address' },
      { name: 'spender', type: 'address' },
    ],
    outputs: [{ name: '', type: 'uint256' }],
  },
  {
    name: 'approve',
    type: 'function',
    stateMutability: 'nonpayable',
    inputs: [
      { name: 'spender', type: 'address' },
      { name: 'value', type: 'uint256' },
    ],
    outputs: [{ name: '', type: 'bool' }],
  },
];

const onrampAbi = [
  {
    name: 'wrap',
    type: 'function',
    stateMutability: 'nonpayable',
    inputs: [
      { name: '_asset', type: 'address' },
      { name: '_to', type: 'address' },
      { name: '_amount', type: 'uint256' },
    ],
    outputs: [],
  },
];

function getEnv(name, fallback = '') {
  return process.env[name] || fallback;
}

function requiredEnv(name) {
  const value = getEnv(name);
  if (!value) {
    throw new Error(`Missing required env: ${name}`);
  }
  return value;
}

function parseArgs(argv) {
  return {
    dryRun: argv.includes('--dry-run'),
  };
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

function encodeProxyTransactionData(transactions) {
  return encodeFunctionData({
    abi: proxyWalletFactoryAbi,
    functionName: 'proxy',
    args: [transactions],
  });
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

function deriveSafeWallet(ownerAddress) {
  return getCreate2Address({
    bytecodeHash: SAFE_INIT_CODE_HASH,
    from: SAFE_FACTORY_ADDRESS,
    salt: keccak256(encodeAbiParameters([{ name: 'address', type: 'address' }], [ownerAddress])),
  });
}

function splitAndPackSig(signature) {
  let sigV = Number.parseInt(signature.slice(-2), 16);
  switch (sigV) {
    case 0:
    case 1:
      sigV += 31;
      break;
    case 27:
    case 28:
      sigV += 4;
      break;
    default:
      throw new Error(`Invalid signature v: ${sigV}`);
  }

  const adjustedSig = signature.slice(0, -2) + sigV.toString(16).padStart(2, '0');
  const r = BigInt(`0x${adjustedSig.slice(2, 66)}`);
  const s = BigInt(`0x${adjustedSig.slice(66, 130)}`);
  const v = Number.parseInt(adjustedSig.slice(130, 132), 16);

  return concat([
    toHex(r, { size: 32 }),
    toHex(s, { size: 32 }),
    toHex(v, { size: 1 }),
  ]);
}

function createSafeStructHash({ chainId, safeAddress, to, value, data, operation, safeTxGas, baseGas, gasPrice, gasToken, refundReceiver, nonce }) {
  return hashTypedData({
    primaryType: 'SafeTx',
    domain: {
      chainId,
      verifyingContract: safeAddress,
    },
    types: {
      SafeTx: [
        { name: 'to', type: 'address' },
        { name: 'value', type: 'uint256' },
        { name: 'data', type: 'bytes' },
        { name: 'operation', type: 'uint8' },
        { name: 'safeTxGas', type: 'uint256' },
        { name: 'baseGas', type: 'uint256' },
        { name: 'gasPrice', type: 'uint256' },
        { name: 'gasToken', type: 'address' },
        { name: 'refundReceiver', type: 'address' },
        { name: 'nonce', type: 'uint256' },
      ],
    },
    message: {
      to,
      value: BigInt(value),
      data,
      operation,
      safeTxGas: BigInt(safeTxGas),
      baseGas: BigInt(baseGas),
      gasPrice: BigInt(gasPrice),
      gasToken,
      refundReceiver,
      nonce: BigInt(nonce),
    },
  });
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

async function submitRelayerTransaction(request, relayerApiKey, relayerApiKeyAddress) {
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

async function buildSafeRequest({ account, proxyWallet, tx, metadata }) {
  const from = toAddress(account.address, 'signer address');
  const safeAddress = deriveSafeWallet(from);
  if (safeAddress.toLowerCase() !== proxyWallet.toLowerCase()) {
    throw new Error(`SAFE address mismatch: env=${proxyWallet} derived=${safeAddress}`);
  }

  const noncePayload = await fetchRelayerJson(`/nonce?address=${from}&type=SAFE`);
  const nonce = String(noncePayload.nonce);
  const safeTxGas = '0';
  const baseGas = '0';
  const gasPrice = '0';
  const gasToken = zeroAddress;
  const refundReceiver = zeroAddress;

  const signature = await account.signMessage({
    message: {
      raw: toBytes(
        createSafeStructHash({
          chainId: 137,
          safeAddress,
          to: tx.to,
          value: tx.value || '0',
          data: tx.data,
          operation: 0,
          safeTxGas,
          baseGas,
          gasPrice,
          gasToken,
          refundReceiver,
          nonce,
        })
      ),
    },
  });

  return {
    from,
    to: tx.to,
    proxyWallet: safeAddress,
    data: tx.data,
    nonce,
    signature: splitAndPackSig(signature),
    signatureParams: {
      gasPrice,
      operation: '0',
      safeTxnGas: safeTxGas,
      baseGas,
      gasToken,
      refundReceiver,
    },
    type: 'SAFE',
    metadata: metadata || '',
  };
}

async function buildProxyRequest({ account, proxyWallet, publicClient, tx, metadata }) {
  const from = toAddress(account.address, 'signer address');
  const relayPayload = await fetchProxyRelayPayload(from);
  const relayAddress = toAddress(relayPayload.address, 'relayer address');
  const nonce = String(relayPayload.nonce);
  const call = {
    to: tx.to,
    typeCode: 1,
    value: BigInt(tx.value || '0'),
    data: tx.data,
  };
  const data = encodeProxyTransactionData([call]);
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

function buildApproveTx() {
  return {
    to: USDCE_ADDRESS,
    value: '0',
    data: encodeFunctionData({
      abi: erc20Abi,
      functionName: 'approve',
      args: [PUSD_ONRAMP_ADDRESS, MAX_UINT256],
    }),
  };
}

function buildWrapTx(proxyWallet, amountRaw) {
  return {
    to: PUSD_ONRAMP_ADDRESS,
    value: '0',
    data: encodeFunctionData({
      abi: onrampAbi,
      functionName: 'wrap',
      args: [USDCE_ADDRESS, proxyWallet, amountRaw],
    }),
  };
}

async function fetchUsdceState(publicClient, proxyWallet) {
  if (!publicClient) {
    throw new Error('POLYGON_RPC_URL is required for auto-wrap worker');
  }

  const [balanceRaw, allowanceRaw] = await Promise.all([
    publicClient.readContract({
      address: USDCE_ADDRESS,
      abi: erc20Abi,
      functionName: 'balanceOf',
      args: [proxyWallet],
    }),
    publicClient.readContract({
      address: USDCE_ADDRESS,
      abi: erc20Abi,
      functionName: 'allowance',
      args: [proxyWallet, PUSD_ONRAMP_ADDRESS],
    }),
  ]);

  return {
    balanceRaw,
    allowanceRaw,
    balance: Number(formatUnits(balanceRaw, USDCE_DECIMALS)),
    allowance: Number(formatUnits(allowanceRaw, USDCE_DECIMALS)),
  };
}

async function executeRelayedTx({ account, proxyWallet, publicClient, relayerApiKey, relayerApiKeyAddress, tx, label }) {
  const signatureType = getEnv('POLY_SIGNATURE_TYPE', '2').trim();
  const signerAddress = toAddress(account.address, 'POLY_PRIVATE_KEY address');
  const relayerOwnerAddress = toAddress(relayerApiKeyAddress, 'RELAYER_API_KEY_ADDRESS');

  const request = signatureType === '2'
    ? await buildSafeRequest({ account, proxyWallet, tx, metadata: label })
    : await buildProxyRequest({ account, proxyWallet, publicClient, tx, metadata: label });

  console.log(`Submitting ${request.type} tx: ${label}`);
  console.log(`Signer: ${signerAddress} | Relayer owner: ${relayerOwnerAddress} | Proxy: ${request.proxyWallet}`);

  const submitResult = await submitRelayerTransaction(request, relayerApiKey, relayerOwnerAddress);
  console.log(`Relayer accepted transaction: id=${submitResult.transactionID} state=${submitResult.state}`);

  const finalTxn = await waitForRelayerTransaction(submitResult.transactionID);
  console.log(`Confirmed: state=${finalTxn.state} hash=${finalTxn.transactionHash || 'pending-hash-unavailable'}`);
  return finalTxn;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const proxyWallet = toAddress(requiredEnv('POLY_PROXY_WALLET'), 'POLY_PROXY_WALLET');
  const privateKey = getEnv('POLY_PRIVATE_KEY');
  const relayerApiKey = getEnv('RELAYER_API_KEY');
  const relayerApiKeyAddress = getEnv('RELAYER_API_KEY_ADDRESS');
  const publicClient = createPublicPolygonClient();
  const minWrap = Number(getEnv('POLY_WRAP_MIN_USDC', String(DEFAULT_MIN_WRAP)));
  let account = null;
  if (privateKey) {
    try {
      account = privateKeyToAccount(privateKey);
    } catch (error) {
      if (!args.dryRun) {
        throw error;
      }
    }
  }

  console.log('=== Wrap Worker ===');
  console.log(`Proxy wallet: ${proxyWallet}`);
  console.log(`Mode: ${args.dryRun ? 'DRY-RUN' : 'LIVE'}`);
  console.log(`Signature type: ${getEnv('POLY_SIGNATURE_TYPE', '2').trim() || '2'}`);
  console.log(`Relayer API key: ${relayerApiKey ? 'SET' : 'MISSING'}`);
  console.log(`Relayer API key address: ${relayerApiKeyAddress ? 'SET' : 'MISSING'}`);
  console.log(`Private key: ${privateKey ? 'SET' : 'MISSING'}`);
  console.log(`Polygon RPC: ${getEnv('POLYGON_RPC_URL') ? 'SET' : 'MISSING'}`);
  console.log(`Min wrap threshold: ${minWrap.toFixed(2)} USDC.e`);

  const state = await fetchUsdceState(publicClient, proxyWallet);
  console.log(`USDC.e balance: ${state.balance.toFixed(6)}`);
  console.log(`USDC.e allowance to onramp: ${state.allowance.toFixed(6)}`);

  if (state.balance + 1e-9 < minWrap) {
    console.log('No pending USDC.e balance large enough to activate.');
    return;
  }

  const txs = [];
  if (state.allowanceRaw < state.balanceRaw) {
    txs.push({ label: 'Approve USDC.e for pUSD onramp', tx: buildApproveTx() });
  }
  txs.push({ label: `Wrap ${state.balance.toFixed(6)} USDC.e to pUSD`, tx: buildWrapTx(proxyWallet, state.balanceRaw) });

  console.log(`Planned activation step(s): ${txs.length}`);
  for (const item of txs) {
    console.log(`- ${item.label}`);
  }

  if (args.dryRun) {
    console.log('Dry-run only. No transactions submitted.');
    return;
  }

  requiredEnv('POLY_PRIVATE_KEY');
  requiredEnv('RELAYER_API_KEY');
  requiredEnv('RELAYER_API_KEY_ADDRESS');
  if (!account) {
    throw new Error('Missing signer account for live wrap');
  }

  const results = [];
  for (const item of txs) {
    results.push(
      await executeRelayedTx({
        account,
        proxyWallet,
        publicClient,
        relayerApiKey,
        relayerApiKeyAddress,
        tx: item.tx,
        label: item.label,
      })
    );
  }

  console.log(`Completed activation transaction(s): ${results.length}`);
}

main().catch((error) => {
  console.error(`Wrap worker failed: ${error.message}`);
  process.exitCode = 1;
});
