// whatsapp_bridge.js

const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const axios = require('axios');

// --- Configuration ---
const FASTAPI_BASE_URL = 'http://localhost:8000'; // Make base URL configurable
const POLLING_INTERVAL_MS = 1000; // Poll every 1 second
const RETRY_INTERVAL_MS = 5000; // Retry connection errors every 5 seconds
// --- End Configuration ---

let isClientReady = false; // Flag to track client readiness

// Initialize the WhatsApp client
const client = new Client({
    authStrategy: new LocalAuth(),
    puppeteer: {
        headless: true, // Recommended for server
        args: ['--no-sandbox', '--disable-setuid-sandbox'] // Often needed in server/container environments
     }
});

// --- Event Handlers ---
client.on('qr', (qr) => {
    console.log('Scan the QR code below:');
    qrcode.generate(qr, { small: true });
});

client.on('ready', () => {
    console.log('WhatsApp client is ready!');
    isClientReady = true;
    // --- Start polling ONLY AFTER client is ready ---
    // Check immediately in case messages queued while starting
    pollForOutgoingMessages();
    // -------------------------------------------------
});

client.on('auth_failure', msg => {
    console.error('AUTHENTICATION FAILURE:', msg);
    isClientReady = false; // Mark as not ready
    // Consider exiting or attempting re-auth
});

client.on('disconnected', (reason) => {
    console.log('Client was logged out:', reason);
    isClientReady = false; // Mark as not ready
    // Exit or attempt re-initialization
    process.exit(1); // Exit on disconnect
});

// Listen for incoming messages
client.on('message', async (message) => {
    // Ignore messages if client isn't ready (might happen during init)
    if (!isClientReady) {
        console.log(`Ignoring message from ${message.from} (client not ready)`);
        return;
    }
    try {
        console.log(`Received message from ${message.from}: "${message.body.substring(0, 50)}..."`);
        // Send the incoming message to FastAPI and await only an acknowledgment.
        await axios.post(`${FASTAPI_BASE_URL}/incoming`, {
            user_id: message.from, // e.g., number@c.us
            message: message.body
        });
        // console.log(`Ack received for message from ${message.from}`); // Can be verbose
    } catch (error) {
        console.error(`Error sending incoming message from ${message.from} to FastAPI:`, error.message || error);
    }
});
// --- End Event Handlers ---


// --- Polling Function ---
async function pollForOutgoingMessages() {
    // Only proceed if client is ready
    if (!isClientReady) {
        console.log('Polling paused: WhatsApp client not ready.');
        // Schedule next check later, gives client time to reconnect/ready
        setTimeout(pollForOutgoingMessages, RETRY_INTERVAL_MS);
        return;
    }

    let nextPollDelay = POLLING_INTERVAL_MS; // Default delay

    try {
        // GET messages waiting to be sent.
        const response = await axios.get(`${FASTAPI_BASE_URL}/outgoing`);
        const messages = response.data.messages; // Expected array

        if (messages && messages.length > 0) {
             console.log(`Polling: Found ${messages.length} message(s) to send.`);
            for (const msg of messages) {
                if (!msg.user_id || msg.message === undefined || !msg.message_id) {
                    console.warn('Polling: Skipping invalid message structure from backend:', msg);
                    continue;
                }
                try {
                    // Send the outgoing message to the correct user.
                    // msg.user_id should now be correctly formatted (e.g., number@c.us) by Python backend
                    console.log(`Sending to ${msg.user_id} (ID: ${msg.message_id}): "${msg.message.substring(0, 50)}..."`);
                    await client.sendMessage(msg.user_id, msg.message);

                    // Notify FastAPI that the message was sent.
                    await axios.post(`${FASTAPI_BASE_URL}/ack`, {
                        user_id: msg.user_id, // Include user_id in ACK for logging on backend
                        message_id: msg.message_id
                    });
                    console.log(`ACK sent for message ID: ${msg.message_id}`);
                } catch (sendError) {
                    // Handle errors during send/ack for a specific message
                    console.error(`Error sending message ID ${msg.message_id} to ${msg.user_id}:`, sendError.message || sendError);
                    // Decide if you want to retry this specific message later or just skip it
                    // For now, we log the error and the loop continues to the next message / next poll cycle.
                    // If the error is 'invalid wid', the user_id format is still wrong from the backend.
                    // If it's another error, it might be a temporary WA issue.
                }
            }
        }
        // else { console.log('Polling: No messages found.'); } // Can be verbose

    } catch (error) {
        // Handle errors during the polling GET request itself
        if (error.code === 'ECONNREFUSED' || error.code === 'ECONNRESET') {
            console.error(`Polling Error: Connection to FastAPI (${FASTAPI_BASE_URL}) refused/reset. Is the backend running?`);
            nextPollDelay = RETRY_INTERVAL_MS; // Wait longer before retrying connection
        } else if (axios.isAxiosError(error)) {
             console.error(`Polling Error: Axios error polling FastAPI - ${error.message} (Status: ${error.response?.status})`);
             if (error.response?.status >= 500) {
                  nextPollDelay = RETRY_INTERVAL_MS; // Wait longer on server errors
             }
        }
         else {
            console.error('Polling Error: Unexpected error during polling:', error);
            nextPollDelay = RETRY_INTERVAL_MS; // Wait longer on unexpected errors
        }
    } finally {
        // Schedule the next poll using the determined delay
        setTimeout(pollForOutgoingMessages, nextPollDelay);
    }
}
// --- End Polling Function ---

// --- Initialization ---
console.log('Initializing WhatsApp client...');
client.initialize().catch(err => {
    console.error('Client initialization failed:', err);
    process.exit(1); // Exit if initialization fails critically
});

console.log('WhatsApp Bridge script started. Waiting for client ready event to start polling...');
// NOTE: Polling now starts inside the 'ready' event handler.
// --- End Initialization ---