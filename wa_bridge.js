// whatsapp_bridge.js

// --- Load Environment Variables ---
// Place this at the very top to load .env file
require('dotenv').config();
// ---------------------------------

const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const axios = require('axios');
const nodemailer = require('nodemailer'); // <-- Add nodemailer

// --- Configuration ---
const FASTAPI_BASE_URL = process.env.FASTAPI_BASE_URL || 'http://localhost:8000';
const POLLING_INTERVAL_MS = parseInt(process.env.POLLING_INTERVAL_MS || '1000', 10);
const RETRY_INTERVAL_MS = parseInt(process.env.RETRY_INTERVAL_MS || '5000', 10);
const CONSECUTIVE_ERROR_THRESHOLD = parseInt(process.env.CONSECUTIVE_ERROR_THRESHOLD || '5', 10); // Exit after 5 consecutive send errors

// --- Email Alert Configuration ---
const ALERT_EMAIL_ENABLED = process.env.ALERT_EMAIL_ENABLED === 'true';
const ALERT_EMAIL_TO = process.env.ALERT_EMAIL_TO || 'erez@mvp-house.com'; // Default recipient
const SMTP_HOST = process.env.SMTP_HOST;
const SMTP_PORT = parseInt(process.env.SMTP_PORT || '587', 10); // Default SMTP port
const SMTP_USER = process.env.SMTP_USER;
const SMTP_PASS = process.env.SMTP_PASS; // Use App Password for Gmail, etc.
const ALERT_FROM_NAME = process.env.ALERT_FROM_NAME || 'WhatsTasker Alert';

let transporter = null;
if (ALERT_EMAIL_ENABLED) {
    if (SMTP_HOST && SMTP_PORT && SMTP_USER && SMTP_PASS) {
        transporter = nodemailer.createTransport({
            host: SMTP_HOST,
            port: SMTP_PORT,
            secure: SMTP_PORT === 465, // true for 465, false for other ports like 587
            auth: {
                user: SMTP_USER,
                pass: SMTP_PASS,
            },
        });
        console.log('Email transporter configured.');
    } else {
        console.error('Email alerting enabled, but SMTP configuration is missing in environment variables! Alerts will not be sent.');
    }
}

// --- Global State ---
let isClientReady = false;
let consecutiveSendErrors = 0; // Counter for send errors

// --- Helper Functions ---

/**
 * Sends an email alert if alerting is enabled and configured.
 * @param {string} subject The email subject.
 * @param {string} body The email body (plain text).
 */
async function sendEmailAlert(subject, body) {
    if (!ALERT_EMAIL_ENABLED || !transporter) {
        // Log locally if email can't be sent
        console.warn(`[ALERT SKIPPED - Email Disabled/Misconfigured] Subject: ${subject}`);
        return;
    }

    const mailOptions = {
        from: `"${ALERT_FROM_NAME}" <${SMTP_USER}>`, // Show friendly name
        to: ALERT_EMAIL_TO,
        subject: subject,
        text: `${body}\n\nTimestamp: ${new Date().toISOString()}`,
    };

    try {
        console.log(`Attempting to send email alert: "${subject}"`);
        let info = await transporter.sendMail(mailOptions);
        console.log('Email alert sent successfully: %s', info.messageId);
    } catch (error) {
        console.error('Error sending email alert:', error);
    }
}

// --- Initialize the WhatsApp client ---
const client = new Client({
    authStrategy: new LocalAuth(), // Uses .wwebjs_auth folder
    puppeteer: {
        headless: true,
        args: ['--no-sandbox', '--disable-setuid-sandbox']
     }
});

// --- Event Handlers ---

client.on('qr', (qr) => {
    console.log('QR RECEIVED. Scan the QR code below:');
    qrcode.generate(qr, { small: true });
    // Send email alert that QR scan is needed
    sendEmailAlert(
        'WhatsTasker Alert: QR Scan Required',
        'The WhatsTasker WhatsApp bridge requires you to scan a QR code.\nPlease check the VPS logs (logs/whatsapp_bridge.log) and scan the code using your linked WhatsApp phone.'
    );
});

client.on('ready', () => {
    console.log('WhatsApp client is ready!');
    isClientReady = true;
    consecutiveSendErrors = 0; // Reset error counter on successful connection
    // Optional: Send an alert that connection is ready (can be noisy)
    // sendEmailAlert('WhatsTasker Info: WhatsApp Bridge Connected', 'The WhatsApp bridge successfully connected and is ready.');
    pollForOutgoingMessages();
});

client.on('auth_failure', async (msg) => { // Make async for await
    console.error('AUTHENTICATION FAILURE:', msg);
    isClientReady = false;
    // Send email alert for auth failure
    await sendEmailAlert( // Use await here
        'WhatsTasker CRITICAL: Authentication Failure',
        `WhatsApp authentication failed.\nReason: ${msg}\nManual intervention (likely deleting session and restarting) required.`
    );
    // Consider exiting after alert? Might depend on the reason.
    // process.exit(1);
});

client.on('disconnected', async (reason) => { // Make async for await
    console.log('Client was logged out/disconnected:', reason);
    isClientReady = false;
    // Send email alert for disconnection *before* exiting
    await sendEmailAlert( // Use await here
        'WhatsTasker Alert: Session Disconnected',
        `The WhatsApp bridge session was disconnected.\nReason: ${reason}\nThe monitor script should attempt to restart the bridge. A QR scan might be required upon restart.`
    );
    process.exit(1); // Exit after attempting to send alert
});

// Listen for incoming messages (no change needed here)
client.on('message', async (message) => {
    if (!isClientReady) {
        console.log(`Ignoring message from ${message.from} (client not ready)`);
        return;
    }
    try {
        console.log(`Received message from ${message.from}: "${message.body.substring(0, 50)}..."`);
        await axios.post(`${FASTAPI_BASE_URL}/incoming`, {
            user_id: message.from,
            message: message.body
        });
    } catch (error) {
        console.error(`Error sending incoming message from ${message.from} to FastAPI:`, error.message || error);
        // Optional: Alert on repeated errors sending incoming messages? Less critical.
    }
});

// --- Polling Function ---
async function pollForOutgoingMessages() {
    if (!isClientReady) {
        console.log('Polling paused: WhatsApp client not ready.');
        setTimeout(pollForOutgoingMessages, RETRY_INTERVAL_MS);
        return;
    }

    let nextPollDelay = POLLING_INTERVAL_MS;

    try {
        const response = await axios.get(`${FASTAPI_BASE_URL}/outgoing`);
        const messages = response.data.messages;

        if (messages && messages.length > 0) {
             console.log(`Polling: Found ${messages.length} message(s) to send.`);
            for (const msg of messages) {
                if (!msg.user_id || msg.message === undefined || !msg.message_id) {
                    console.warn('Polling: Skipping invalid message structure from backend:', msg);
                    continue; // Skip invalid message
                }
                try {
                    console.log(`Sending to ${msg.user_id} (ID: ${msg.message_id}): "${msg.message.substring(0, 50)}..."`);
                    await client.sendMessage(msg.user_id, msg.message);

                    // --- Reset error counter on successful send ---
                    consecutiveSendErrors = 0;
                    // ---------------------------------------------

                    // Notify FastAPI that the message was sent (ACK)
                    await axios.post(`${FASTAPI_BASE_URL}/ack`, {
                        user_id: msg.user_id,
                        message_id: msg.message_id
                    });
                    console.log(`ACK sent for message ID: ${msg.message_id}`);

                } catch (sendError) {
                    console.error(`Error sending message ID ${msg.message_id} to ${msg.user_id}:`, sendError.message || sendError);

                    // --- Check for "Session closed" and handle ---
                    if (sendError.message && sendError.message.includes('Session closed')) {
                        consecutiveSendErrors++;
                        console.warn(`Consecutive send errors: ${consecutiveSendErrors}/${CONSECUTIVE_ERROR_THRESHOLD}`);
                        if (consecutiveSendErrors >= CONSECUTIVE_ERROR_THRESHOLD) {
                            console.error(`Exceeded consecutive send error threshold (${CONSECUTIVE_ERROR_THRESHOLD}). Exiting.`);
                            // Send alert *before* exiting
                            await sendEmailAlert( // Use await
                                'WhatsTasker CRITICAL: Repeated Send Errors (Session Closed)',
                                `The WhatsApp bridge encountered ${consecutiveSendErrors} consecutive 'Session closed' errors while trying to send messages.\nLast failed message ID: ${msg.message_id} to ${msg.user_id}\nThis indicates the WhatsApp connection is broken.\nThe bridge process is now exiting and should be restarted by the monitor. A QR scan will likely be required.`
                            );
                            process.exit(1); // Exit to trigger monitor restart
                        }
                    }
                    // ---------------------------------------------
                    // Note: We don't retry sending the *same* failed message here.
                    // It remains in the Python queue until an ACK is sent.
                    // The loop continues to the next message (if any) or the next poll.
                }
            } // End for loop
        } // End if messages

    } catch (error) {
        // Reset error counter if the error is related to polling FastAPI, not sending a WA message
        consecutiveSendErrors = 0;
        // Handle polling GET errors (same as before)
        if (error.code === 'ECONNREFUSED' || error.code === 'ECONNRESET') {
            console.error(`Polling Error: Connection to FastAPI (${FASTAPI_BASE_URL}) refused/reset. Is the backend running?`);
            nextPollDelay = RETRY_INTERVAL_MS;
        } else if (axios.isAxiosError(error)) {
             console.error(`Polling Error: Axios error polling FastAPI - ${error.message} (Status: ${error.response?.status})`);
             if (error.response?.status >= 500) {
                  nextPollDelay = RETRY_INTERVAL_MS;
             }
        } else {
            console.error('Polling Error: Unexpected error during polling:', error);
            nextPollDelay = RETRY_INTERVAL_MS;
        }
    } finally {
        // Only schedule next poll if the process hasn't exited
        // (The process.exit above might terminate before this runs)
        if (!process.exitCode) { // Check if exit code hasn't been set
             setTimeout(pollForOutgoingMessages, nextPollDelay);
        }
    }
}

// --- Initialization ---
console.log('Initializing WhatsApp client...');
client.initialize().catch(async (err) => { // Make async for await
    console.error('Client initialization failed:', err);
    // Send alert on critical init failure
    await sendEmailAlert( // Use await
        'WhatsTasker CRITICAL: Bridge Initialization Failed',
        `The wa_bridge.js script failed to initialize the WhatsApp client.\nError: ${err.message || err}\nThe script will exit. Check VPS logs and resources.`
    );
    process.exit(1); // Exit if initialization fails critically
});

console.log('WhatsApp Bridge script started. Waiting for client ready event to start polling...');

// --- Add graceful shutdown handling ---
process.on('SIGINT', async () => {
    console.log('\nSIGINT received. Shutting down bridge...');
    await sendEmailAlert('WhatsTasker Info: Bridge Shutting Down (SIGINT)', 'The WhatsApp bridge process received SIGINT and is shutting down.');
    // Optional: await client.destroy(); // May not be necessary if exiting
    process.exit(0);
});

process.on('SIGTERM', async () => {
    console.log('SIGTERM received. Shutting down bridge...');
    await sendEmailAlert('WhatsTasker Info: Bridge Shutting Down (SIGTERM)', 'The WhatsApp bridge process received SIGTERM and is shutting down.');
    // Optional: await client.destroy();
    process.exit(0);
});
// -----------------------------------