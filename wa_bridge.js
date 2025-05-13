// whatsapp_bridge.js

// --- Dependencies ---
const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js'); // Added MessageMedia just in case, not used yet
const qrcode = require('qrcode-terminal');
const axios = require('axios');
require('dotenv').config(); // For environment variables if you add a .env file later

// --- Configuration (Consider moving more to .env later) ---
const FASTAPI_BASE_URL = process.env.FASTAPI_BASE_URL || 'http://localhost:8000';
const POLLING_INTERVAL_MS = parseInt(process.env.POLLING_INTERVAL_MS, 10) || 1000;
const RETRY_INTERVAL_MS = parseInt(process.env.RETRY_INTERVAL_MS, 10) || 5000; // For backend connection retries
const MAX_SEND_RETRIES_PER_MESSAGE = parseInt(process.env.MAX_SEND_RETRIES_PER_MESSAGE, 10) || 3;
const SEND_RETRY_DELAY_MS = parseInt(process.env.SEND_RETRY_DELAY_MS, 10) || 2000;
const MAX_ACK_RETRIES = parseInt(process.env.MAX_ACK_RETRIES, 10) || 3;
const ACK_RETRY_DELAY_MS = parseInt(process.env.ACK_RETRY_DELAY_MS, 10) || 2000;
const CONSECUTIVE_POLLING_ERROR_THRESHOLD = parseInt(process.env.CONSECUTIVE_POLLING_ERROR_THRESHOLD, 10) || 10; // Exit after 10 consecutive polling errors

// --- State Variables ---
let isClientReady = false;
let consecutivePollingErrors = 0;
let clientInstance; // To store the client for access in shutdown handler

// --- Custom Logger ---
function logMessage(level, message, ...optionalParams) {
    const now = new Date();
    const options = { timeZone: 'Asia/Jerusalem', hour12: false, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3 };
    const timestamp = now.toLocaleString('en-CA', options).replace(/, /g, ' ').replace(/\//g, '-'); // YYYY-MM-DD HH:MM:SS.mmm
    const levelStr = level.toUpperCase();
    
    // Construct the log prefix including the [PID]
    const logPrefix = `[${timestamp} IDT] [PID:${process.pid}] [${levelStr}]`;

    if (levelStr === 'ERROR' || levelStr === 'WARN') {
        console[level.toLowerCase() || 'log'](logPrefix, message, ...optionalParams);
    } else {
        console.log(logPrefix, message, ...optionalParams);
    }
}


// --- Utility: Sleep Function ---
function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// --- Initialize the WhatsApp client ---
const client = new Client({
    authStrategy: new LocalAuth({dataPath: '.wwebjs_auth'}), // Specify dataPath
    puppeteer: {
        headless: true,
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage', // Often helps in constrained environments
            '--disable-accelerated-2d-canvas',
            '--no-first-run',
            '--no-zygote',
            // '--single-process', // Disables GPU sandbox, use with caution if other methods fail
            '--disable-gpu'
        ]
    },
    // Increase takeover timeout if needed, default is 60s
    // takeoverTimeoutMs: 120000, 
});
clientInstance = client; // Store for shutdown handler

// --- Event Handlers ---
client.on('qr', (qr) => {
    logMessage('INFO', 'QR Code Received. Scan with WhatsApp:');
    qrcode.generate(qr, { small: true });
});

client.on('ready', async () => {
    logMessage('INFO', `WhatsApp client is ready! Logged in as: ${client.info.pushname} (${client.info.wid.user})`);
    isClientReady = true;
    consecutivePollingErrors = 0; // Reset error counter on successful ready
    await pollForOutgoingMessages(); // Start polling
});

client.on('authenticated', () => {
    logMessage('INFO', 'WhatsApp client authenticated successfully.');
});

client.on('auth_failure', msg => {
    logMessage('ERROR', 'AUTHENTICATION FAILURE:', msg);
    isClientReady = false;
    logMessage('ERROR', 'Exiting due to authentication failure.');
    process.exit(1); // Critical failure, exit
});

client.on('disconnected', (reason) => {
    logMessage('WARN', 'Client was logged out/disconnected:', reason);
    isClientReady = false;
    logMessage('ERROR', 'Exiting due to disconnection.');
    process.exit(1); // Critical failure, exit
});

client.on('loading_screen', (percent, message) => {
    logMessage('INFO', `Loading WhatsApp Web: ${percent}% - ${message}`);
});

client.on('change_state', state => {
    logMessage('INFO', `WhatsApp client state changed: ${state}`);
});

client.on('error', err => {
    logMessage('ERROR', 'Unhandled WhatsApp client error:', err);
    // Consider exiting if error is critical, e.g., related to Puppeteer crashing
    if (err.message && (err.message.includes('Protocol error') || err.message.includes('Page crashed'))) {
        logMessage('ERROR', 'Critical Puppeteer/Protocol error detected. Exiting.');
        process.exit(1);
    }
});

// Listen for incoming messages
client.on('message', async (message) => {
    if (!isClientReady) {
        logMessage('WARN', `Ignoring incoming message from ${message.from} (client not ready).`);
        return;
    }
    if (message.isStatus) { // Ignore status updates
        logMessage('DEBUG', `Ignoring status update from ${message.from}.`);
        return;
    }
    if (message.type === 'revoked') { // Ignore revoked messages
        logMessage('DEBUG', `Ignoring revoked message from ${message.from}.`);
        return;
    }


    try {
        const chat = await message.getChat();
        if (chat.isGroup) {
            logMessage('INFO', `Ignoring message from group: "${chat.name}" by ${message.author || message.from}`);
            return; // Ignore group messages for now
        }

        logMessage('INFO', `Received message from ${message.from}: "${message.body.substring(0, 50)}..."`);
        await axios.post(`${FASTAPI_BASE_URL}/incoming`, {
            user_id: message.from,
            message: message.body
        });
        // logMessage('DEBUG', `Ack received for incoming message from ${message.from}`);
    } catch (error) {
        logMessage('ERROR', `Error sending incoming message from ${message.from} to FastAPI:`, error.response ? error.response.data : error.message);
    }
});

// --- Polling Function ---
async function pollForOutgoingMessages() {
    if (!isClientReady) {
        logMessage('WARN', 'Polling paused: WhatsApp client not ready.');
        setTimeout(pollForOutgoingMessages, RETRY_INTERVAL_MS);
        return;
    }

    let nextPollDelay = POLLING_INTERVAL_MS;

    try {
        const response = await axios.get(`${FASTAPI_BASE_URL}/outgoing`, { timeout: 5000 }); // Added timeout for GET
        const messages = response.data.messages;
        consecutivePollingErrors = 0; // Reset on successful poll

        if (messages && messages.length > 0) {
            logMessage('INFO', `Polling: Found ${messages.length} message(s) to send.`);
            for (const msg of messages) {
                if (!msg.user_id || msg.message === undefined || !msg.message_id) {
                    logMessage('WARN', 'Polling: Skipping invalid message structure from backend:', msg);
                    continue;
                }

                let sentSuccessfully = false;
                for (let attempt = 1; attempt <= MAX_SEND_RETRIES_PER_MESSAGE; attempt++) {
                    try {
                        logMessage('INFO', `Attempt ${attempt}/${MAX_SEND_RETRIES_PER_MESSAGE} sending to ${msg.user_id} (ID: ${msg.message_id}): "${msg.message.substring(0, 50)}..."`);
                        await client.sendMessage(msg.user_id, msg.message);
                        sentSuccessfully = true;
                        logMessage('INFO', `Message ID ${msg.message_id} sent successfully to ${msg.user_id}.`);
                        break; // Exit retry loop on success
                    } catch (sendError) {
                        logMessage('ERROR', `Send attempt ${attempt} for message ID ${msg.message_id} to ${msg.user_id} FAILED:`, sendError.message);
                        if (sendError.message && (sendError.message.includes('Session closed') || sendError.message.includes('Page crashed') || sendError.message.includes('invalid wid'))) {
                            logMessage('ERROR', 'Critical send error. Not retrying this message. Exiting bridge.');
                            process.exit(1); // Exit for critical, non-retryable send errors
                        }
                        if (attempt < MAX_SEND_RETRIES_PER_MESSAGE) {
                            logMessage('INFO', `Waiting ${SEND_RETRY_DELAY_MS / 1000}s before next send attempt...`);
                            await sleep(SEND_RETRY_DELAY_MS);
                        } else {
                            logMessage('ERROR', `All ${MAX_SEND_RETRIES_PER_MESSAGE} send attempts FAILED for message ID ${msg.message_id}.`);
                        }
                    }
                }

                if (sentSuccessfully) {
                    let ackSentSuccessfully = false;
                    for (let ackAttempt = 1; ackAttempt <= MAX_ACK_RETRIES; ackAttempt++) {
                        try {
                            // logMessage('DEBUG', `Attempt ${ackAttempt}/${MAX_ACK_RETRIES} sending ACK for message ID: ${msg.message_id}`);
                            await axios.post(`${FASTAPI_BASE_URL}/ack`, {
                                user_id: msg.user_id,
                                message_id: msg.message_id
                            }, { timeout: 3000 }); // Added timeout for ACK
                            ackSentSuccessfully = true;
                            logMessage('INFO', `ACK sent successfully for message ID: ${msg.message_id}`);
                            break; // Exit ACK retry loop on success
                        } catch (ackError) {
                            logMessage('ERROR', `ACK attempt ${ackAttempt} for message ID ${msg.message_id} FAILED:`, ackError.response ? ackError.response.data : ackError.message);
                            if (ackAttempt < MAX_ACK_RETRIES) {
                                logMessage('INFO', `Waiting ${ACK_RETRY_DELAY_MS / 1000}s before next ACK attempt...`);
                                await sleep(ACK_RETRY_DELAY_MS);
                            } else {
                                logMessage('CRITICAL', `All ${MAX_ACK_RETRIES} ACK attempts FAILED for message ID ${msg.message_id}. Message sent but backend may not know.`);
                                // This is a problematic state. What to do? For now, just log critically.
                            }
                        }
                    }
                }
            }
        }
    } catch (error) {
        consecutivePollingErrors++;
        logMessage('ERROR', `Polling Error (Attempt ${consecutivePollingErrors}/${CONSECUTIVE_POLLING_ERROR_THRESHOLD}):`, error.code || error.message);
        if (error.response) {
            logMessage('ERROR', 'Polling Error Response Data:', error.response.data);
        }

        if (error.code === 'ECONNREFUSED' || error.code === 'ECONNRESET' || (error.response && error.response.status >= 500)) {
            nextPollDelay = RETRY_INTERVAL_MS;
        }

        if (consecutivePollingErrors >= CONSECUTIVE_POLLING_ERROR_THRESHOLD) {
            logMessage('CRITICAL', `Reached ${CONSECUTIVE_POLLING_ERROR_THRESHOLD} consecutive polling errors. Exiting bridge.`);
            process.exit(1);
        }
    } finally {
        if (isClientReady && !_stopPollingFlag) { // Check flag before setting next timeout
            setTimeout(pollForOutgoingMessages, nextPollDelay);
        }
    }
}

// --- Initialization ---
logMessage('INFO', `WhatsApp Bridge script starting. Target Backend: ${FASTAPI_BASE_URL}`);
logMessage('INFO', 'Initializing WhatsApp client...');
client.initialize().catch(err => {
    logMessage('ERROR', 'Client initialization failed:', err);
    process.exit(1);
});

// --- Graceful Shutdown Handling ---
let _stopPollingFlag = false; // Flag to signal polling loop to stop

async function shutdownBridge(signal) {
    if (_stopPollingFlag) return; // Already shutting down
    _stopPollingFlag = true;
    logMessage('WARN', `Received ${signal}, initiating graceful shutdown...`);
    isClientReady = false; // Stop processing new events

    if (clientInstance) {
        try {
            logMessage('INFO', 'Attempting to destroy WhatsApp client session...');
            await clientInstance.destroy();
            logMessage('INFO', 'WhatsApp client session destroyed.');
        } catch (e) {
            logMessage('ERROR', 'Error destroying client during shutdown:', e.message);
        }
    }
    logMessage('INFO', 'Bridge shutdown complete. Exiting.');
    process.exit(0);
}

process.on('SIGINT', () => shutdownBridge('SIGINT'));
process.on('SIGTERM', () => shutdownBridge('SIGTERM'));
process.on('SIGQUIT', () => shutdownBridge('SIGQUIT'));

// Polling now starts via the 'ready' event