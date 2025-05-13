// whatsapp_bridge.js

// --- Dependencies ---
const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const axios = require('axios');
// const dotenv = require('dotenv'); // If you were to use a .env for these constants
// dotenv.config();

// --- Configuration ---
const FASTAPI_BASE_URL = process.env.FASTAPI_BASE_URL || 'http://localhost:8000';
const POLLING_INTERVAL_MS = parseInt(process.env.POLLING_INTERVAL_MS, 10) || 1000;
const RETRY_INTERVAL_MS = parseInt(process.env.RETRY_INTERVAL_MS, 10) || 5000;
const MAX_SEND_RETRIES_PER_MESSAGE = parseInt(process.env.MAX_SEND_RETRIES_PER_MESSAGE, 10) || 3;
const SEND_RETRY_DELAY_MS = parseInt(process.env.SEND_RETRY_DELAY_MS, 10) || 2000;
const MAX_ACK_RETRIES = parseInt(process.env.MAX_ACK_RETRIES, 10) || 3;
const ACK_RETRY_DELAY_MS = parseInt(process.env.ACK_RETRY_DELAY_MS, 10) || 2000;
const CONSECUTIVE_POLLING_ERROR_THRESHOLD = parseInt(process.env.CONSECUTIVE_POLLING_ERROR_THRESHOLD, 10) || 10;
// --- NEW: Timeout for POST to /incoming ---
const INCOMING_POST_TIMEOUT_MS = parseInt(process.env.INCOMING_POST_TIMEOUT_MS, 10) || 10000; // 10 seconds

// --- State Variables ---
let isClientReady = false;
let consecutivePollingErrors = 0;
let clientInstance;
let _stopPollingFlag = false; // To control polling loop termination

// --- Custom Logger (from your provided file) ---
function logMessage(level, message, ...optionalParams) {
    const now = new Date();
    // Using a simplified timestamp for brevity, adjust as needed
    const timestamp = now.toISOString();
    const levelStr = level.toUpperCase();
    const logPrefix = `[${timestamp}] [PID:${process.pid}] [${levelStr}] [wa_bridge]`;

    if (optionalParams.length > 0 && optionalParams[optionalParams.length -1] instanceof Error) {
        const err = optionalParams.pop();
        console[level.toLowerCase() || 'log'](logPrefix, message, ...optionalParams, err.message, err.stack ? `\nStack: ${err.stack}` : '');
    } else {
        console[level.toLowerCase() || 'log'](logPrefix, message, ...optionalParams);
    }
}

// --- Utility: Sleep Function ---
function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// --- Initialize the WhatsApp client ---
const client = new Client({
    authStrategy: new LocalAuth({ dataPath: '.wwebjs_auth' }),
    puppeteer: {
        headless: true,
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-accelerated-2d-canvas',
            '--no-first-run',
            '--no-zygote',
            '--disable-gpu'
        ]
    },
});
clientInstance = client;

// --- Event Handlers ---
client.on('qr', (qr) => {
    logMessage('INFO', 'QR Code Received. Scan with WhatsApp:');
    qrcode.generate(qr, { small: true });
});

client.on('ready', async () => {
    logMessage('INFO', `WhatsApp client is ready! Logged in as: ${client.info.pushname} (${client.info.wid.user})`);
    isClientReady = true;
    consecutivePollingErrors = 0;
    if (!_stopPollingFlag) { // Ensure polling doesn't start if already shutting down
        pollForOutgoingMessages(); // Start polling immediately
    }
});

client.on('authenticated', () => { logMessage('INFO', 'WhatsApp client authenticated successfully.'); });
client.on('auth_failure', msg => {
    logMessage('ERROR', 'AUTHENTICATION FAILURE:', msg);
    isClientReady = false;
    logMessage('ERROR', 'Exiting due to authentication failure.');
    shutdownBridge('AUTH_FAILURE_EXIT', 1); // Exit with code 1
});
client.on('disconnected', (reason) => {
    logMessage('WARN', 'Client was logged out/disconnected:', reason);
    isClientReady = false;
    logMessage('ERROR', 'Exiting due to disconnection.');
    shutdownBridge('DISCONNECTED_EXIT', 1); // Exit with code 1
});
client.on('loading_screen', (percent, message) => { logMessage('INFO', `Loading WhatsApp Web: ${percent}% - ${message}`); });
client.on('change_state', state => { logMessage('INFO', `WhatsApp client state changed: ${state}`); });
client.on('error', err => {
    logMessage('ERROR', 'Unhandled WhatsApp client error:', err);
    if (err.message && (err.message.includes('Protocol error') || err.message.includes('Page crashed'))) {
        logMessage('ERROR', 'Critical Puppeteer/Protocol error detected. Exiting.');
        shutdownBridge('PUPPETEER_CRASH_EXIT', 1); // Exit with code 1
    }
});

// Listen for incoming messages
client.on('message', async (message) => {
    if (_stopPollingFlag || !isClientReady) {
        logMessage('WARN', `Ignoring incoming message from ${message.from} (client not ready or shutting down).`);
        return;
    }
    if (message.isStatus || message.type === 'revoked') {
        // logMessage('DEBUG', `Ignoring status/revoked message from ${message.from}.`);
        return;
    }

    try {
        const chat = await message.getChat();
        if (chat.isGroup) {
            // logMessage('INFO', `Ignoring message from group: "${chat.name}" by ${message.author || message.from}`);
            return;
        }

        logMessage('INFO', `Received message from ${message.from}: "${message.body.substring(0, 50)}..."`);
        // --- MODIFIED: Added timeout to POST /incoming ---
        await axios.post(`${FASTAPI_BASE_URL}/incoming`, {
            user_id: message.from,
            message: message.body
        }, { timeout: INCOMING_POST_TIMEOUT_MS }); // Configurable timeout
        // logMessage('DEBUG', `ACK received for incoming message from ${message.from}`);
    } catch (error) {
        let errMsg = error.message;
        if (error.response) errMsg = JSON.stringify(error.response.data) || error.message;
        else if (error.code) errMsg = `${error.code}: ${error.message}`;

        logMessage('ERROR', `Error sending incoming message from ${message.from} to FastAPI:`, errMsg, error);
        // If the error is a timeout specifically for this POST, log it clearly
        if (error.code === 'ECONNABORTED' && error.message.includes('timeout')) {
            logMessage('ERROR', `POST to /incoming for ${message.from} timed out after ${INCOMING_POST_TIMEOUT_MS / 1000}s.`);
        }
    }
});

// --- Polling Function ---
async function pollForOutgoingMessages() {
    if (_stopPollingFlag) {
        logMessage('INFO', 'Polling stopped by flag.');
        return;
    }
    if (!isClientReady) {
        logMessage('WARN', 'Polling paused: WhatsApp client not ready.');
        if (!_stopPollingFlag) setTimeout(pollForOutgoingMessages, RETRY_INTERVAL_MS);
        return;
    }

    let nextPollDelay = POLLING_INTERVAL_MS;

    try {
        const response = await axios.get(`${FASTAPI_BASE_URL}/outgoing`, { timeout: 5000 });
        const messages = response.data.messages;
        if (consecutivePollingErrors > 0) { // Log restoration if errors were occurring
            logMessage('INFO', `Polling successful. Connection to backend restored. Resetting error count.`);
        }
        consecutivePollingErrors = 0;

        if (messages && messages.length > 0) {
            logMessage('INFO', `Polling: Found ${messages.length} message(s) to send.`);
            for (const msg of messages) {
                if (!msg.user_id || msg.message === undefined || !msg.message_id) {
                    logMessage('WARN', 'Polling: Skipping invalid message structure from backend:', msg);
                    continue;
                }

                let sentSuccessfully = false;
                for (let attempt = 1; attempt <= MAX_SEND_RETRIES_PER_MESSAGE; attempt++) {
                    if (_stopPollingFlag) break; // Check flag before attempting send
                    try {
                        logMessage('INFO', `Attempt ${attempt}/${MAX_SEND_RETRIES_PER_MESSAGE} sending to ${msg.user_id} (ID: ${msg.message_id}): "${msg.message.substring(0, 50)}..."`);
                        await client.sendMessage(msg.user_id, msg.message);
                        sentSuccessfully = true;
                        logMessage('INFO', `Message ID ${msg.message_id} sent successfully to ${msg.user_id}.`);
                        break;
                    } catch (sendError) {
                        logMessage('ERROR', `Send attempt ${attempt} for message ID ${msg.message_id} to ${msg.user_id} FAILED:`, sendError.message, sendError);
                        if (sendError.message && (sendError.message.includes('Session closed') || sendError.message.includes('Page crashed') || sendError.message.includes('invalid wid'))) {
                            logMessage('ERROR', 'Critical send error. Not retrying this message. Triggering shutdown.');
                            shutdownBridge('CRITICAL_SEND_ERROR', 1);
                            return; // Stop further processing
                        }
                        if (attempt < MAX_SEND_RETRIES_PER_MESSAGE && !_stopPollingFlag) {
                            logMessage('INFO', `Waiting ${SEND_RETRY_DELAY_MS / 1000}s before next send attempt...`);
                            await sleep(SEND_RETRY_DELAY_MS);
                        } else if (attempt >= MAX_SEND_RETRIES_PER_MESSAGE) {
                            logMessage('ERROR', `All ${MAX_SEND_RETRIES_PER_MESSAGE} send attempts FAILED for message ID ${msg.message_id}.`);
                        }
                    }
                } // End send retry loop

                if (_stopPollingFlag) break; // Check flag after send attempts

                if (sentSuccessfully) {
                    let ackSentSuccessfully = false;
                    for (let ackAttempt = 1; ackAttempt <= MAX_ACK_RETRIES; ackAttempt++) {
                        if (_stopPollingFlag) break; // Check flag before attempting ACK
                        try {
                            await axios.post(`${FASTAPI_BASE_URL}/ack`, {
                                user_id: msg.user_id,
                                message_id: msg.message_id
                            }, { timeout: 3000 });
                            ackSentSuccessfully = true;
                            logMessage('INFO', `ACK sent successfully for message ID: ${msg.message_id}`);
                            break;
                        } catch (ackError) {
                            logMessage('ERROR', `ACK attempt ${ackAttempt} for message ID ${msg.message_id} FAILED:`, (ackError.response ? JSON.stringify(ackError.response.data) : ackError.message), ackError);
                            if (ackAttempt < MAX_ACK_RETRIES && !_stopPollingFlag) {
                                logMessage('INFO', `Waiting ${ACK_RETRY_DELAY_MS / 1000}s before next ACK attempt...`);
                                await sleep(ACK_RETRY_DELAY_MS);
                            } else if (ackAttempt >= MAX_ACK_RETRIES) {
                                logMessage('CRITICAL', `All ${MAX_ACK_RETRIES} ACK attempts FAILED for message ID ${msg.message_id}. Message sent but backend may not know.`);
                            }
                        }
                    } // End ACK retry loop
                } // End if sentSuccessfully
                 if (_stopPollingFlag) break; // Check flag after processing a message
            } // End for...of messages loop
        } // End if messages
    } catch (error) {
        consecutivePollingErrors++;
        let errMsg = error.message;
        if (error.response) errMsg = `${error.response.status}: ${JSON.stringify(error.response.data) || error.message}`;
        else if (error.code) errMsg = `${error.code}: ${error.message}`;

        logMessage('ERROR', `Polling Error (Attempt ${consecutivePollingErrors}/${CONSECUTIVE_POLLING_ERROR_THRESHOLD}):`, errMsg, error);

        if (error.code === 'ECONNREFUSED' || error.code === 'ECONNRESET' || error.code === 'ENOTFOUND' || (error.response && error.response.status >= 500)) {
            nextPollDelay = RETRY_INTERVAL_MS; // Wait longer for server-side or connection refused issues
        } else if (error.code === 'ECONNABORTED' || (error.isAxiosError && error.message.toLowerCase().includes('timeout'))) {
            nextPollDelay = POLLING_INTERVAL_MS * 2; // Slightly longer delay for timeouts if not server error
        }


        if (consecutivePollingErrors >= CONSECUTIVE_POLLING_ERROR_THRESHOLD) {
            logMessage('CRITICAL', `Reached ${CONSECUTIVE_POLLING_ERROR_THRESHOLD} consecutive polling errors. Triggering shutdown.`);
            shutdownBridge('MAX_POLLING_ERRORS', 1);
            return; // Stop polling
        }
    } finally {
        if (isClientReady && !_stopPollingFlag) {
            setTimeout(pollForOutgoingMessages, nextPollDelay);
        } else if (!isClientReady && !_stopPollingFlag) { // If client disconnected but not shutting down, retry later
            setTimeout(pollForOutgoingMessages, RETRY_INTERVAL_MS);
        }
    }
}

// --- Initialization ---
logMessage('INFO', `WhatsApp Bridge script starting. Target Backend: ${FASTAPI_BASE_URL}`);
logMessage('INFO', 'Initializing WhatsApp client...');
client.initialize().catch(err => {
    logMessage('ERROR', 'Client initialization failed:', err, err);
    shutdownBridge('INIT_FAILURE',1); // Ensure shutdown on init failure
});

// --- Graceful Shutdown Handling ---
async function shutdownBridge(signal, exitCode = 0) {
    if (_stopPollingFlag) {
        logMessage('INFO', 'Shutdown already in progress.');
        return;
    }
    _stopPollingFlag = true; // Signal polling loop and other async ops to stop
    logMessage('WARN', `Received ${signal}, initiating graceful shutdown...`);
    isClientReady = false;

    if (clientInstance && typeof clientInstance.destroy === 'function') {
        try {
            logMessage('INFO', 'Attempting to destroy WhatsApp client session...');
            await clientInstance.destroy();
            logMessage('INFO', 'WhatsApp client session destroyed.');
        } catch (e) {
            logMessage('ERROR', 'Error destroying client during shutdown:', e.message, e);
        }
    } else {
        logMessage('WARN', 'Client instance not available or destroy method missing for shutdown.');
    }
    logMessage('INFO', `Bridge shutdown complete. Exiting with code ${exitCode}.`);
    process.exit(exitCode);
}

process.on('SIGINT', () => shutdownBridge('SIGINT'));
process.on('SIGTERM', () => shutdownBridge('SIGTERM'));
process.on('SIGQUIT', () => shutdownBridge('SIGQUIT')); // Often used by `kill <pid>`
process.on('uncaughtException', (error) => {
    logMessage('CRITICAL', 'Uncaught Exception:', error.message, error);
    shutdownBridge('UNCAUGHT_EXCEPTION', 1);
});
process.on('unhandledRejection', (reason, promise) => {
    logMessage('CRITICAL', 'Unhandled Rejection at:', promise, 'reason:', reason instanceof Error ? reason.message : reason, reason instanceof Error ? reason : undefined);
    shutdownBridge('UNHANDLED_REJECTION', 1);
});

// Polling starts via the 'ready' event handler