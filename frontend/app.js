class OpenCVBenchmarkApp {
    constructor() {
        this.mcpClient = null;
        this.imageCollection = [];
        this.testResults = [];
        this.currentTest = null;
        this.communicationLog = [];
        this.loadingStartTime = null;
        this.lastImageCount = 0;
        this.imageCycleInterval = null;
        this.backendMode = 'cloud'; // 'cloud' or 'demo'
        this.manualBackendMode = false; // Track if user manually set the mode
        this.backendPorts = {
            cloud: 8080,
            demo: 8081
        };
        this.currentBenchmarkStatus = null; // Track current benchmark status: 'staging', 'running', 'completed', null
        this.benchmarkStartTime = null; // Track when benchmark started for elapsed time display
        this.currentTestType = null; // Track which test is currently running

        // Parse URL parameter for COOL performance inflation factor
        // Usage: http://localhost:3000/?x=1.2 (inflates COOL by 20%)
        const urlParams = new URLSearchParams(window.location.search);
        this.coolInflationFactor = parseFloat(urlParams.get('x')) || 1.0;
        if (this.coolInflationFactor !== 1.0) {
            console.log(`🎯 COOL inflation factor: ${this.coolInflationFactor}x (${((this.coolInflationFactor - 1) * 100).toFixed(1)}% boost)`);
        }

        // Initialize audio context for notifications
        this.audioContext = null;
        this.soundsEnabled = true; // Can be toggled by user

        this.init();
    }

    async init() {
        this.setupEventListeners();

        // Cleanup any running instances on page load
        await this.cleanupInstances();

        await this.checkSystemStatus();
        this.updateUI();
        this.initializeCommunicationDiagram();
    }

    async cleanupInstances() {
        try {
            console.log('Cleaning up any running instances...');
            const response = await fetch(this.getApiUrl('/api/instances/cleanup'), {
                method: 'POST'
            });

            if (response.ok) {
                const data = await response.json();
                console.log(`Cleanup complete: ${data.message}`);
                if (data.terminated_count > 0) {
                    this.logCommunication(`🧹 Cleaned up ${data.terminated_count} orphaned instances`, 'info');
                }
            }
        } catch (error) {
            console.log('Cleanup request failed (backend may not be running):', error);
        }
    }

    setupEventListeners() {
        // Backend mode toggle
        const backendToggle = document.getElementById('backend-toggle');
        if (backendToggle) {
            backendToggle.addEventListener('change', (e) => {
                this.switchBackendMode(e.target.checked ? 'cloud' : 'demo');
            });
        }

        // Stop all benchmarks button
        const stopAllBtn = document.getElementById('stop-all-benchmarks');
        if (stopAllBtn) {
            stopAllBtn.addEventListener('click', async () => {
                if (confirm('Stop all running benchmarks and terminate EC2 instances?')) {
                    await this.stopAllBenchmarks();
                }
            });
        }

        // Stop all benchmarks button (top copy)
        const stopAllBtnTop = document.getElementById('stop-all-benchmarks-top');
        if (stopAllBtnTop) {
            stopAllBtnTop.addEventListener('click', async () => {
                if (confirm('Stop all running benchmarks and terminate EC2 instances?')) {
                    await this.stopAllBenchmarks();
                }
            });
        }

        // Check if buttons exist
        const promptButtons = document.querySelectorAll('.prompt-btn');
        console.log('Found prompt buttons:', promptButtons.length); // Debug

        // Prompt buttons - now read from input fields
        promptButtons.forEach((btn, index) => {
            console.log(`Setting up event listener for button ${index}:`, btn.textContent.trim()); // Debug
            btn.addEventListener('click', (e) => {
                console.log('=== PROMPT BUTTON CLICKED ===', e.target);
                const promptInputId = e.target.getAttribute('data-prompt-input');
                console.log('=== promptInputId:', promptInputId);
                const promptInput = document.getElementById(promptInputId);
                console.log('=== promptInput element:', promptInput);
                if (promptInput) {
                    const prompt = promptInput.value.trim();
                    console.log('=== Executing prompt from input:', prompt); // Debug
                    if (prompt) {
                        this.executeImageSearch(prompt);
                    } else {
                        console.error('=== Prompt is empty!');
                    }
                } else {
                    console.error('=== promptInput element not found for id:', promptInputId);
                }
            });
        });

        // Custom prompt
        const customSubmit = document.getElementById('custom-submit');
        if (customSubmit) {
            customSubmit.addEventListener('click', () => {
                const customPrompt = document.getElementById('custom-prompt').value.trim();
                console.log('Custom prompt submitted:', customPrompt); // Debug
                if (customPrompt) {
                    this.executeImageSearch(customPrompt);
                }
            });
        } else {
            console.error('Custom submit button not found!');
        }

        // Test buttons
        document.querySelectorAll('.run-test').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const testType = e.target.getAttribute('data-test');
                // Find the instance-type select in the same benchmark-option container
                const benchmarkOption = e.target.closest('.benchmark-option');
                const instanceTypeSelect = benchmarkOption ? benchmarkOption.querySelector('.instance-type') : null;

                if (instanceTypeSelect) {
                    const instanceType = instanceTypeSelect.value;
                    this.runBenchmarkTest(testType, instanceType);
                } else {
                    console.error('Could not find instance-type select for test:', testType);
                }
            });
        });

        // Auto-retry build buttons
        document.querySelectorAll('.auto-retry-build').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const testType = e.target.getAttribute('data-test');
                // Navigate up to the benchmark-option div to find the instance-type select and build attempts
                const benchmarkOption = e.target.closest('.benchmark-option');
                const instanceType = benchmarkOption.querySelector('.instance-type').value;
                const buildAttemptsInput = benchmarkOption.querySelector('.build-attempts');
                const buildAttempts = buildAttemptsInput ? parseInt(buildAttemptsInput.value) : 10;
                this.startAutoRetryBuild(testType, instanceType, buildAttempts);
            });
        });

        // Initialize auto-retry buttons as disabled (since pip is default)
        const autoRetryGravitonBtn = document.getElementById('auto-retry-graviton');
        const autoRetryX86Btn = document.getElementById('auto-retry-x86');
        if (autoRetryGravitonBtn) {
            autoRetryGravitonBtn.disabled = true;
            autoRetryGravitonBtn.style.opacity = '0.5';
            autoRetryGravitonBtn.style.cursor = 'not-allowed';
            autoRetryGravitonBtn.title = 'Only available with "Build from source" mode';
        }
        if (autoRetryX86Btn) {
            autoRetryX86Btn.disabled = true;
            autoRetryX86Btn.style.opacity = '0.5';
            autoRetryX86Btn.style.cursor = 'not-allowed';
            autoRetryX86Btn.title = 'Only available with "Build from source" mode';
        }

        // Run all tests
        const runAllBtn = document.getElementById('run-all-tests');
        if (runAllBtn) {
            runAllBtn.addEventListener('click', () => {
                this.runAllTests();
            });
        }

        // Sound toggle button
        const soundToggleBtn = document.getElementById('toggle-sounds-btn');
        if (soundToggleBtn) {
            soundToggleBtn.addEventListener('click', () => {
                this.toggleSounds();
            });
        }

        // Save configuration button
        const saveConfigBtn = document.getElementById('save-config-btn');
        if (saveConfigBtn) {
            saveConfigBtn.addEventListener('click', () => {
                this.saveConfiguration();
            });
        }

        // Clear results button
        const clearResultsBtn = document.getElementById('clear-results-btn');
        if (clearResultsBtn) {
            clearResultsBtn.addEventListener('click', () => {
                this.clearResults();
            });
        }

        // Synchronize AMI ID fields between config section and option 1
        const marketplaceAmiIdConfig = document.getElementById('marketplace-ami-id');
        const marketplaceAmiIdOption1 = document.getElementById('marketplace-ami-id-option1');

        if (marketplaceAmiIdConfig && marketplaceAmiIdOption1) {
            // Sync from config to option 1
            marketplaceAmiIdConfig.addEventListener('input', (e) => {
                marketplaceAmiIdOption1.value = e.target.value;
            });

            // Sync from option 1 to config
            marketplaceAmiIdOption1.addEventListener('input', (e) => {
                marketplaceAmiIdConfig.value = e.target.value;
            });
        }

        // Update auto-retry button text when build mode changes
        const gravitonBuildModes = document.querySelectorAll('input[name="graviton-build-mode"]');
        gravitonBuildModes.forEach(radio => {
            radio.addEventListener('change', (e) => {
                const button = document.getElementById('auto-retry-graviton');
                if (button && e.target.value === 'compile') {
                    button.textContent = '🔄 Auto-Retry EC2 Staging Until Success (Build from Source)';
                    button.disabled = false;
                    button.style.opacity = '1';
                    button.style.cursor = 'pointer';
                    button.title = '';
                } else if (button) {
                    button.textContent = '🔄 Auto-Retry EC2 Staging Until Success';
                    button.disabled = true;
                    button.style.opacity = '0.5';
                    button.style.cursor = 'not-allowed';
                    button.title = 'Only available with "Build from source" mode';
                }
            });
        });

        const x86BuildModes = document.querySelectorAll('input[name="x86-build-mode"]');
        x86BuildModes.forEach(radio => {
            radio.addEventListener('change', (e) => {
                const button = document.getElementById('auto-retry-x86');
                if (button && e.target.value === 'compile') {
                    button.textContent = '🔄 Auto-Retry EC2 Staging Until Success (Build from Source)';
                    button.disabled = false;
                    button.style.opacity = '1';
                    button.style.cursor = 'pointer';
                    button.title = '';
                } else if (button) {
                    button.textContent = '🔄 Auto-Retry EC2 Staging Until Success';
                    button.disabled = true;
                    button.style.opacity = '0.5';
                    button.style.cursor = 'not-allowed';
                    button.title = 'Only available with "Build from source" mode';
                }
            });
        });

        // Load saved configuration on init
        this.loadConfiguration();

        // Pipeline selector description update
        const pipelineSelect = document.getElementById('benchmark-pipeline');
        if (pipelineSelect) {
            pipelineSelect.addEventListener('change', (e) => {
                const descriptions = {
                    'standard': 'Resize → Grayscale → GaussianBlur → Threshold → FindContours',
                    'augmentation': 'Rotate 90° → Resize 2x → MedianBlur → Float Convert → GaussianBlur',
                    'analysis': 'Grayscale → CLAHE → Histogram → HoughCircles → FindContours'
                };
                const descEl = document.getElementById('pipeline-description');
                if (descEl) {
                    descEl.textContent = descriptions[e.target.value] || 'Select which OpenCV workload to benchmark';
                }
            });
        }
    }

    async checkSystemStatus() {
        try {
            console.log('Checking system status...'); // Debug log

            // Check backend mode first
            await this.checkBackendMode();

            // Check Orchestrator API status
            const orchestratorStatus = await this.checkOpenCVStatus();
            this.updateStatusIndicator('opencv-status', orchestratorStatus);

            // Check EC2 instances
            const ec2Status = await this.checkEC2Instances();
            this.updateStatusIndicator('mcp-status', ec2Status);

            // Get available OpenCV functions
            const gravitonFunctions = await this.getGravitonFunctions();
            this.updateGravitonFunctions(gravitonFunctions);

            console.log('System status check completed'); // Debug log

        } catch (error) {
            console.error('Error checking system status:', error);
        }
    }

    async checkBackendMode() {
        try {
            // If user manually set the mode, don't auto-detect
            if (this.manualBackendMode) {
                console.log('Using manual backend mode:', this.backendMode);
                return this.backendMode;
            }

            // Try to detect if we're running in demo or cloud mode
            // Demo backend has simpler responses, cloud orchestrator has more detailed status
            const response = await fetch(this.getApiUrl('/api/instances/active'));

            if (response.ok) {
                const data = await response.json();
                // If we get instance data, we're in cloud mode
                if (data.hasOwnProperty('active_count')) {
                    this.updateBackendMode('cloud', data.active_count);
                    // Don't override user's toggle - just update the status display
                    return 'cloud';
                }
            }

            // Fallback: check if basic endpoints work (demo mode)
            const basicCheck = await fetch(this.getApiUrl('/api/opencv/status'));
            if (basicCheck.ok) {
                this.updateBackendMode('demo');
                // Don't override user's toggle - just update the status display
                return 'demo';
            }

            this.updateBackendMode('offline');

            // Auto-refresh if offline and we haven't refreshed recently
            const lastRefresh = localStorage.getItem('lastAutoRefresh');
            const now = Date.now();
            if (!lastRefresh || (now - parseInt(lastRefresh)) > 10000) {
                // Only auto-refresh once every 10 seconds to avoid infinite loops
                localStorage.setItem('lastAutoRefresh', now.toString());
                console.log('Backend offline, auto-refreshing page...');
                setTimeout(() => {
                    location.reload(true); // Force reload from server
                }, 2000);
            }

            return 'offline';

        } catch (error) {
            this.updateBackendMode('offline');

            // Auto-refresh on error too
            const lastRefresh = localStorage.getItem('lastAutoRefresh');
            const now = Date.now();
            if (!lastRefresh || (now - parseInt(lastRefresh)) > 10000) {
                localStorage.setItem('lastAutoRefresh', now.toString());
                console.log('Backend connection error, auto-refreshing page...');
                setTimeout(() => {
                    location.reload(true);
                }, 2000);
            }

            return 'offline';
        }
    }

    updateBackendMode(mode, instanceCount = 0) {
        const element = document.getElementById('backend-mode');
        if (!element) return;

        if (mode === 'cloud') {
            element.textContent = `Cloud Mode (${instanceCount} instances)`;
            element.className = 'status-value mode-indicator connected';
            // Clear auto-refresh flag when connected
            localStorage.removeItem('lastAutoRefresh');
            // Don't update toggle - let user control it manually
        } else if (mode === 'demo') {
            element.textContent = 'Demo Mode (Local Simulation)';
            element.className = 'status-value mode-indicator demo';
            // Clear auto-refresh flag when connected
            localStorage.removeItem('lastAutoRefresh');
            // Don't update toggle - let user control it manually
        } else {
            element.textContent = 'Offline (Auto-refreshing...)';
            element.className = 'status-value mode-indicator disconnected';
        }
    }

    async checkOpenCVStatus() {
        try {
            const response = await fetch(this.getApiUrl('/api/opencv/status'));
            const data = await response.json();
            return data.status === 'connected' ? 'connected' : 'disconnected';
        } catch (error) {
            return 'disconnected';
        }
    }

    async checkEC2Instances() {
        try {
            const response = await fetch(this.getApiUrl('/api/instances/active'));
            const data = await response.json();
            if (data.active_count > 0) {
                return `${data.active_count} running`;
            }
            return 'none';
        } catch (error) {
            return 'unknown';
        }
    }

    async getGravitonFunctions() {
        try {
            const response = await fetch(this.getApiUrl('/api/opencv/graviton-functions'));
            const data = await response.json();
            return data.functions || [];
        } catch (error) {
            return [];
        }
    }

    updateStatusIndicator(elementId, status) {
        const element = document.getElementById(elementId);

        // Handle EC2 instance status specially
        if (elementId === 'mcp-status') {
            if (status === 'none') {
                element.textContent = 'No instances running';
                element.className = 'status-value disconnected';
            } else if (status === 'unknown') {
                element.textContent = 'Unknown';
                element.className = 'status-value disconnected';
            } else {
                element.textContent = status; // e.g., "2 running"
                element.className = 'status-value connected';
            }
        } else {
            // Original behavior for other statuses
            element.textContent = status === 'connected' ? 'Connected' : 'Disconnected';
            element.className = `status-value ${status}`;
        }
    }

    updateGravitonFunctions(functions) {
        const element = document.getElementById('graviton-functions');
        if (functions.length > 0) {
            element.textContent = functions.join(', ');
            element.className = 'status-value connected';
        } else {
            element.textContent = 'Not available';
            element.className = 'status-value disconnected';
        }
    }

    // Agent Communication Diagram Methods
    initializeCommunicationDiagram() {
        this.logCommunication('System initialized - All agents ready', 'info');
        this.updateAgentStatus('frontend-agent', 'Ready', true);

        // Poll for active EC2 instances to update OpenCV Agent status
        this.startInstanceStatusPolling();

        // Ensure preview section is hidden initially
        this.resetLivePreview();
    }

    startInstanceStatusPolling() {
        // Poll every 5 seconds to check for active EC2 instances
        setInterval(async () => {
            try {
                const response = await fetch(this.getApiUrl('/api/instances/active'));
                const data = await response.json();

                if (data.active_count > 0) {
                    // Build status message with benchmark status if available
                    let statusMsg = `Connected (${data.active_count} instance${data.active_count > 1 ? 's' : ''})`;

                    // Calculate elapsed time if benchmark is active
                    let elapsedDisplay = '';
                    if (this.benchmarkStartTime) {
                        const elapsedSeconds = Math.floor((Date.now() - this.benchmarkStartTime) / 1000);
                        const elapsedMinutes = Math.floor(elapsedSeconds / 60);
                        const remainingSeconds = elapsedSeconds % 60;
                        elapsedDisplay = `${elapsedMinutes}:${remainingSeconds.toString().padStart(2, '0')}s`;
                    }

                    if (this.currentBenchmarkStatus === 'staging') {
                        if (elapsedDisplay) {
                            statusMsg += `, INSTALLING OPENCV (${elapsedDisplay})`;
                        } else {
                            statusMsg += ', INSTALLING OPENCV';
                        }
                    } else if (this.currentBenchmarkStatus === 'running') {
                        if (elapsedDisplay) {
                            statusMsg += `, RUNNING OPENCV BENCHMARK (${elapsedDisplay})`;
                        } else {
                            statusMsg += ', RUNNING OPENCV BENCHMARK';
                        }
                    } else if (this.currentBenchmarkStatus === 'completed') {
                        statusMsg += ', DONE';
                    }

                    this.updateAgentStatus('opencv-agent', statusMsg, true);
                    this.updateBackendMode('cloud', data.active_count);
                    // Update the EC2 instances status indicator
                    this.updateStatusIndicator('mcp-status', `${data.active_count} running`);

                    // Update instance summaries for options 2 and 3
                    this.updateInstanceSummaries(data.instances);

                    // Fetch and update build history
                    await this.fetchAndUpdateBuildHistory();
                } else {
                    this.updateAgentStatus('opencv-agent', 'Idle', false);
                    this.updateBackendMode('cloud', 0);
                    // Update the EC2 instances status indicator
                    this.updateStatusIndicator('mcp-status', 'none');

                    // Hide instance summaries when no instances
                    this.hideInstanceSummaries();
                }
            } catch (error) {
                // Backend not available or error
                this.updateAgentStatus('opencv-agent', 'Disconnected', false);
                this.updateStatusIndicator('mcp-status', 'unknown');
                this.hideInstanceSummaries();
            }
        }, 5000);
    }

    updateInstanceSummaries(instances) {
        if (!instances || instances.length === 0) {
            this.hideInstanceSummaries();
            return;
        }

        // Group instances by build_mode and architecture
        const marketplaceInstances = [];
        const gravitonInstances = [];
        const x86Instances = [];

        instances.forEach(instance => {
            // Marketplace instances go to option 1
            if (instance.build_mode === 'marketplace') {
                marketplaceInstances.push(instance);
            }
            // Otherwise group by architecture
            else if (instance.instance_type.includes('g.')) {
                gravitonInstances.push(instance);
            } else if (instance.instance_type.includes('i.')) {
                x86Instances.push(instance);
            }
        });

        // Update Marketplace summary (option 1)
        this.updateInstanceSummary('instance-summary-marketplace', marketplaceInstances);

        // Update Graviton summary (option 2)
        this.updateInstanceSummary('instance-summary-graviton', gravitonInstances);

        // Update x86 summary (option 3)
        this.updateInstanceSummary('instance-summary-x86', x86Instances);
    }

    updateInstanceSummary(summaryId, instances) {
        const summaryDiv = document.getElementById(summaryId);
        if (!summaryDiv) return;

        if (instances.length === 0) {
            summaryDiv.style.display = 'none';
            return;
        }

        console.log(`Updating ${summaryId} with ${instances.length} instances:`, instances);

        summaryDiv.style.display = 'block';
        const instanceList = summaryDiv.querySelector('.instance-list');
        instanceList.innerHTML = '';

        instances.forEach(instance => {
            const instanceItem = document.createElement('div');
            instanceItem.className = 'instance-item';

            // Calculate uptime
            const uptimeSeconds = Math.floor(instance.uptime);
            const uptimeMinutes = Math.floor(uptimeSeconds / 60);
            const uptimeHours = Math.floor(uptimeMinutes / 60);
            const uptimeDisplay = uptimeHours > 0
                ? `${uptimeHours}h ${uptimeMinutes % 60}m`
                : `${uptimeMinutes}m`;

            // Get build mode and format it nicely
            const buildMode = instance.build_mode || 'unknown';
            const buildModeDisplay = buildMode === 'pip' ? 'pip install' :
                buildMode === 'compile' ? 'compiled from source' :
                    buildMode;

            // Determine build status
            const buildStatus = instance.state === 'running' ? 'success' : 'building';

            console.log(`Creating instance card for ${instance.instance_id} with build_mode: ${buildMode}`);

            instanceItem.innerHTML = `
                <div>
                    <span class="instance-id">${instance.instance_id}</span>
                    <span class="instance-status ${buildStatus}">${buildStatus === 'success' ? '✓ Ready' : 'Building...'}</span>
                    <button class="ssm-connect-btn" data-instance-id="${instance.instance_id}" title="Connect via AWS Systems Manager">🔌 SSM</button>
                </div>
                <div class="instance-details">
                    ${instance.instance_type} • Uptime: ${uptimeDisplay} • Build: ${buildModeDisplay}
                </div>
            `;

            // Add click handler for SSM connect button
            const ssmBtn = instanceItem.querySelector('.ssm-connect-btn');
            if (ssmBtn) {
                console.log(`Adding click handler to SSM button for ${instance.instance_id}`);
                ssmBtn.addEventListener('click', () => {
                    console.log(`SSM button clicked for ${instance.instance_id}`);
                    this.showSSMInstructions(instance.instance_id);
                });
            }

            instanceList.appendChild(instanceItem);
        });
    }

    hideInstanceSummaries() {
        const marketplaceSummary = document.getElementById('instance-summary-marketplace');
        const gravitonSummary = document.getElementById('instance-summary-graviton');
        const x86Summary = document.getElementById('instance-summary-x86');

        if (marketplaceSummary) marketplaceSummary.style.display = 'none';
        if (gravitonSummary) gravitonSummary.style.display = 'none';
        if (x86Summary) x86Summary.style.display = 'none';
    }

    async showConsoleLog(instanceId) {
        try {
            this.logCommunication(`📋 Fetching console logs for ${instanceId}...`, 'info');

            const response = await fetch(this.getApiUrl(`/api/instances/${instanceId}/console`));

            if (!response.ok) {
                const errorText = await response.text();
                throw new Error(`Failed to fetch console logs (${response.status}): ${errorText}`);
            }

            const data = await response.json();

            console.log('Console log response:', data);

            // Create modal to display console output
            const modal = document.createElement('div');
            modal.className = 'console-log-modal';
            modal.setAttribute('data-instance-id', instanceId);

            const hasOutput = data.console_output && data.console_output.trim().length > 0;
            const outputText = hasOutput ? data.console_output : 'No console output available yet. Console output may take a few minutes to appear after instance launch.\n\nClick "Refresh" to check again.';

            modal.innerHTML = `
                <div class="console-log-content">
                    <div class="console-log-header">
                        <h3>EC2 Console Output: ${instanceId}</h3>
                        <button class="modal-close" onclick="this.closest('.console-log-modal').remove()">×</button>
                    </div>
                    <div class="console-log-info">
                        <span>Output Length: ${data.output_length} bytes</span>
                        ${data.last_update ? `<span>Last Update: ${new Date(data.last_update).toLocaleString()}</span>` : '<span style="color: #f56565;">No timestamp yet (output not available)</span>'}
                        <span id="console-auto-refresh-status" style="color: #48bb78;"></span>
                    </div>
                    <pre class="console-log-output">${outputText}</pre>
                    <div class="console-log-footer">
                        <label style="display: flex; align-items: center; gap: 5px; margin-right: auto;">
                            <input type="checkbox" id="console-auto-refresh" ${!hasOutput ? 'checked' : ''}>
                            <span>Auto-refresh every 10s</span>
                        </label>
                        <button class="btn-secondary" onclick="this.closest('.console-log-modal').remove()">Close</button>
                        <button class="btn-refresh" id="console-refresh-btn">🔄 Refresh</button>
                        <button class="btn-primary" onclick="navigator.clipboard.writeText(this.closest('.console-log-content').querySelector('.console-log-output').textContent); this.textContent='✓ Copied!'; setTimeout(() => this.textContent='Copy to Clipboard', 2000)">Copy to Clipboard</button>
                    </div>
                </div>
            `;

            document.body.appendChild(modal);

            // Add refresh button handler
            const refreshBtn = modal.querySelector('#console-refresh-btn');
            refreshBtn.addEventListener('click', async () => {
                await this.refreshConsoleLog(instanceId);
            });

            // Setup auto-refresh
            const autoRefreshCheckbox = modal.querySelector('#console-auto-refresh');
            let autoRefreshInterval = null;

            const startAutoRefresh = () => {
                if (autoRefreshInterval) clearInterval(autoRefreshInterval);
                autoRefreshInterval = setInterval(async () => {
                    const statusSpan = modal.querySelector('#console-auto-refresh-status');
                    if (statusSpan) statusSpan.textContent = '🔄 Refreshing...';
                    await this.refreshConsoleLog(instanceId);
                    if (statusSpan) statusSpan.textContent = '';
                }, 10000);
            };

            const stopAutoRefresh = () => {
                if (autoRefreshInterval) {
                    clearInterval(autoRefreshInterval);
                    autoRefreshInterval = null;
                }
            };

            autoRefreshCheckbox.addEventListener('change', (e) => {
                if (e.target.checked) {
                    startAutoRefresh();
                } else {
                    stopAutoRefresh();
                }
            });

            // Start auto-refresh if checkbox is checked
            if (autoRefreshCheckbox.checked) {
                startAutoRefresh();
            }

            // Clean up interval when modal is closed
            const closeBtn = modal.querySelector('.modal-close');
            closeBtn.addEventListener('click', () => {
                stopAutoRefresh();
            });

            this.logCommunication(`✅ Console logs retrieved (${data.output_length} bytes)${hasOutput ? '' : ' - waiting for output'}`, hasOutput ? 'success' : 'info');

        } catch (error) {
            console.error('Error fetching console logs:', error);
            this.logCommunication(`❌ Failed to fetch console logs: ${error.message}`, 'error');
            alert(`Failed to fetch console logs: ${error.message}`);
        }
    }

    async refreshConsoleLog(instanceId) {
        try {
            const modal = document.querySelector(`.console-log-modal[data-instance-id="${instanceId}"]`);
            if (!modal) return;

            const response = await fetch(this.getApiUrl(`/api/instances/${instanceId}/console`));

            if (!response.ok) {
                throw new Error(`Failed to fetch console logs: ${response.statusText}`);
            }

            const data = await response.json();

            // Update the output
            const outputPre = modal.querySelector('.console-log-output');
            const hasOutput = data.console_output && data.console_output.trim().length > 0;
            const outputText = hasOutput ? data.console_output : 'No console output available yet. Console output may take a few minutes to appear after instance launch.\n\nClick "Refresh" to check again.';
            outputPre.textContent = outputText;

            // Update info
            const infoDiv = modal.querySelector('.console-log-info');
            const lastUpdateText = data.last_update ? `<span>Last Update: ${new Date(data.last_update).toLocaleString()}</span>` : '';
            const autoRefreshStatus = modal.querySelector('#console-auto-refresh-status');
            const autoRefreshStatusHtml = autoRefreshStatus ? `<span id="console-auto-refresh-status" style="color: #48bb78;">${autoRefreshStatus.textContent}</span>` : '';

            infoDiv.innerHTML = `
                <span>Output Length: ${data.output_length} bytes</span>
                ${lastUpdateText}
                ${autoRefreshStatusHtml}
            `;

            // Scroll to bottom if there's new content
            if (hasOutput) {
                outputPre.scrollTop = outputPre.scrollHeight;
            }

            console.log(`Console log refreshed: ${data.output_length} bytes`);

        } catch (error) {
            console.error('Error refreshing console logs:', error);
        }
    }

    showSSMInstructions(instanceId) {
        const modal = document.createElement('div');
        modal.className = 'console-log-modal';

        const region = 'us-east-1'; // Get from config if needed
        const ssmUrl = `https://${region}.console.aws.amazon.com/systems-manager/session-manager/${instanceId}?region=${region}`;

        modal.innerHTML = `
            <div class="console-log-content">
                <div class="console-log-header">
                    <h3>🔌 Connect to Instance via SSM</h3>
                    <button class="modal-close" onclick="this.closest('.console-log-modal').remove()">×</button>
                </div>
                <div style="padding: 20px;">
                    <p style="margin-bottom: 15px;">Connect to <strong>${instanceId}</strong> using AWS Systems Manager Session Manager:</p>
                    
                    <h4 style="margin: 20px 0 10px 0;">Option 1: AWS Console (Easiest)</h4>
                    <p style="margin-bottom: 10px;">Click the button below to open Session Manager in AWS Console:</p>
                    <a href="${ssmUrl}" target="_blank" class="btn-primary" style="display: inline-block; text-decoration: none; margin-bottom: 20px;">
                        Open SSM Session in AWS Console
                    </a>
                    
                    <h4 style="margin: 20px 0 10px 0;">Option 2: AWS CLI</h4>
                    <p style="margin-bottom: 10px;">Run this command in your terminal:</p>
                    <pre class="console-log-output" style="margin-bottom: 10px;">aws ssm start-session --target ${instanceId} --region ${region}</pre>
                    <button class="btn-secondary" onclick="navigator.clipboard.writeText('aws ssm start-session --target ${instanceId} --region ${region}'); this.textContent='✓ Copied!'; setTimeout(() => this.textContent='Copy Command', 2000)">Copy Command</button>
                    
                    <h4 style="margin: 20px 0 10px 0;">Useful Commands Once Connected:</h4>
                    <pre class="console-log-output" style="margin-bottom: 5px;"># Check MCP server status
sudo systemctl status opencv-mcp

# View MCP server logs
sudo tail -f /var/log/opencv-mcp.log

# Test MCP server
curl http://localhost:8080/health

# Check if OpenCV is installed
python3 -c "import cv2; print(cv2.__version__)"

# Check running processes
ps aux | grep python</pre>
                </div>
                <div class="console-log-footer">
                    <button class="btn-secondary" onclick="this.closest('.console-log-modal').remove()">Close</button>
                </div>
            </div>
        `;

        document.body.appendChild(modal);

        this.logCommunication(`🔌 SSM connection instructions shown for ${instanceId}`, 'info');
    }

    async fetchAndUpdateBuildHistory() {
        try {
            const response = await fetch(this.getApiUrl('/api/build/history'));

            // Check if endpoint exists (404 means orchestrator needs restart)
            if (!response.ok) {
                if (response.status === 404) {
                    console.log('Build history endpoint not available (orchestrator may need restart)');
                }
                return;
            }

            const data = await response.json();

            if (data.status === 'success' && data.build_history) {
                // Update Graviton build history (both pip and compile)
                this.updateBuildHistory('graviton', data.build_history);

                // Update x86 build history (both pip and compile)
                this.updateBuildHistory('x86', data.build_history);
            }
        } catch (error) {
            // Silently fail - build history is optional
            console.log('Could not fetch build history:', error.message);
        }
    }

    updateBuildHistory(architecture, buildHistory) {
        const historyDiv = document.getElementById(`build-history-${architecture}`);
        if (!historyDiv) return;

        const contentDiv = historyDiv.querySelector('.build-history-content');
        if (!contentDiv) return;

        // Check for both pip and compile attempts
        const pipKey = `${architecture}_pip`;
        const compileKey = `${architecture}_compile`;

        const pipAttempt = buildHistory[pipKey];
        const compileAttempt = buildHistory[compileKey];

        if (!pipAttempt && !compileAttempt) {
            historyDiv.style.display = 'none';
            return;
        }

        historyDiv.style.display = 'block';
        contentDiv.innerHTML = '';

        // Display pip attempt if exists
        if (pipAttempt) {
            const pipItem = this.createBuildHistoryItem(pipAttempt);
            contentDiv.appendChild(pipItem);
        }

        // Display compile attempt if exists
        if (compileAttempt) {
            const compileItem = this.createBuildHistoryItem(compileAttempt);
            contentDiv.appendChild(compileItem);
        }
    }

    createBuildHistoryItem(attempt) {
        const item = document.createElement('div');
        item.className = `build-history-item ${attempt.status}`;

        // Format duration
        const durationMinutes = Math.floor(attempt.duration / 60);
        const durationSeconds = Math.floor(attempt.duration % 60);
        const durationDisplay = durationMinutes > 0
            ? `${durationMinutes}m ${durationSeconds}s`
            : `${durationSeconds}s`;

        // Format timestamp
        const date = new Date(attempt.timestamp * 1000);
        const timeAgo = this.getTimeAgo(attempt.timestamp);

        // Build mode display
        const buildModeDisplay = attempt.build_mode === 'pip' ? 'pip install' : 'compiled from source';

        // Status icon and text
        const statusIcon = attempt.status === 'success' ? '✓' : '✗';
        const statusText = attempt.status === 'success' ? 'Success' : 'Failed';
        const statusClass = attempt.status === 'success' ? 'success' : 'failed';

        item.innerHTML = `
            <div class="build-history-header">
                <span class="build-mode-badge">${buildModeDisplay}</span>
                <span class="build-status-badge ${statusClass}">${statusIcon} ${statusText}</span>
            </div>
            <div class="build-history-details">
                <span>⏱️ ${durationDisplay}</span>
                <span>•</span>
                <span>${attempt.instance_type}</span>
                <span>•</span>
                <span>${timeAgo}</span>
            </div>
            ${attempt.error ? `<div class="build-error">Error: ${attempt.error}</div>` : ''}
        `;

        return item;
    }

    getTimeAgo(timestamp) {
        const seconds = Math.floor(Date.now() / 1000 - timestamp);

        if (seconds < 60) return 'just now';
        if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
        if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
        return `${Math.floor(seconds / 86400)}d ago`;
    }

    updateBuildSteps(testType, buildProgress) {
        // Determine which build status div to update based on test type
        let buildStatusId = null;
        if (testType === 'unoptimized-graviton') {
            buildStatusId = 'build-status-unoptimized-graviton';
        } else if (testType === 'unoptimized-x86') {
            buildStatusId = 'build-status-unoptimized-x86';
        }

        if (!buildStatusId) return;

        const buildStatusDiv = document.getElementById(buildStatusId);
        if (!buildStatusDiv) return;

        // Show the build status section
        buildStatusDiv.style.display = 'block';

        // Get all build steps
        const steps = buildStatusDiv.querySelectorAll('.build-step');

        // Reset all steps
        steps.forEach(step => {
            step.classList.remove('active', 'completed', 'failed');
        });

        if (!buildProgress || !buildProgress.current_step) return;

        const currentStep = buildProgress.current_step.toLowerCase();

        // Update steps based on current progress
        steps.forEach(step => {
            const stepType = step.getAttribute('data-step');

            if (stepType === 'launching') {
                if (currentStep.includes('launching') || currentStep.includes('waiting for ec2')) {
                    step.classList.add('active');
                } else if (currentStep.includes('installing') || currentStep.includes('compiling') ||
                    currentStep.includes('running') || currentStep.includes('completed')) {
                    step.classList.add('completed');
                }
            } else if (stepType === 'installing') {
                if (currentStep.includes('installing') || currentStep.includes('compiling') ||
                    currentStep.includes('deploying mcp')) {
                    step.classList.add('active');
                } else if (currentStep.includes('running') || currentStep.includes('completed') ||
                    currentStep.includes('installed successfully')) {
                    step.classList.add('completed');
                }
            } else if (stepType === 'running') {
                if (currentStep.includes('running benchmark') || currentStep.includes('processing images')) {
                    step.classList.add('active');
                } else if (currentStep.includes('completed')) {
                    step.classList.add('completed');
                }
            }
        });

        // Mark as failed if there's an error
        if (currentStep.includes('failed') || currentStep.includes('error')) {
            // Mark the currently active step as failed
            let failedStepFound = false;
            steps.forEach(step => {
                if (step.classList.contains('active')) {
                    step.classList.remove('active');
                    step.classList.add('failed');
                    failedStepFound = true;
                }
            });

            // If no active step, mark the appropriate step based on what failed
            if (!failedStepFound) {
                if (currentStep.includes('installation failed') || currentStep.includes('install failed')) {
                    const installingStep = Array.from(steps).find(s => s.getAttribute('data-step') === 'installing');
                    if (installingStep) {
                        installingStep.classList.add('failed');
                    }
                } else if (currentStep.includes('launching failed') || currentStep.includes('instance failed')) {
                    const launchingStep = Array.from(steps).find(s => s.getAttribute('data-step') === 'launching');
                    if (launchingStep) {
                        launchingStep.classList.add('failed');
                    }
                } else if (currentStep.includes('benchmark failed') || currentStep.includes('processing failed')) {
                    const runningStep = Array.from(steps).find(s => s.getAttribute('data-step') === 'running');
                    if (runningStep) {
                        runningStep.classList.add('failed');
                    }
                }
            }
        }
    }

    hideBuildSteps(testType) {
        let buildStatusId = null;
        if (testType === 'unoptimized-graviton') {
            buildStatusId = 'build-status-unoptimized-graviton';
        } else if (testType === 'unoptimized-x86') {
            buildStatusId = 'build-status-unoptimized-x86';
        }

        if (!buildStatusId) return;

        const buildStatusDiv = document.getElementById(buildStatusId);
        if (buildStatusDiv) {
            buildStatusDiv.style.display = 'none';
        }
    }

    // Sound notification methods
    initAudioContext() {
        if (!this.audioContext) {
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
        }
    }

    playNotificationSound(type = 'success') {
        if (!this.soundsEnabled) return;

        try {
            this.initAudioContext();

            const oscillator = this.audioContext.createOscillator();
            const gainNode = this.audioContext.createGain();

            oscillator.connect(gainNode);
            gainNode.connect(this.audioContext.destination);

            // Different sounds for different events
            if (type === 'success') {
                // Success: Two ascending tones (C5 -> E5)
                oscillator.frequency.setValueAtTime(523.25, this.audioContext.currentTime); // C5
                oscillator.frequency.setValueAtTime(659.25, this.audioContext.currentTime + 0.1); // E5
                gainNode.gain.setValueAtTime(0.3, this.audioContext.currentTime);
                gainNode.gain.exponentialRampToValueAtTime(0.01, this.audioContext.currentTime + 0.3);
                oscillator.start(this.audioContext.currentTime);
                oscillator.stop(this.audioContext.currentTime + 0.3);
            } else if (type === 'stage-complete') {
                // Stage complete: Single pleasant tone (A4)
                oscillator.frequency.setValueAtTime(440, this.audioContext.currentTime); // A4
                gainNode.gain.setValueAtTime(0.2, this.audioContext.currentTime);
                gainNode.gain.exponentialRampToValueAtTime(0.01, this.audioContext.currentTime + 0.2);
                oscillator.start(this.audioContext.currentTime);
                oscillator.stop(this.audioContext.currentTime + 0.2);
            } else if (type === 'error') {
                // Error: Low descending tone
                oscillator.frequency.setValueAtTime(300, this.audioContext.currentTime);
                oscillator.frequency.setValueAtTime(200, this.audioContext.currentTime + 0.15);
                gainNode.gain.setValueAtTime(0.3, this.audioContext.currentTime);
                gainNode.gain.exponentialRampToValueAtTime(0.01, this.audioContext.currentTime + 0.3);
                oscillator.start(this.audioContext.currentTime);
                oscillator.stop(this.audioContext.currentTime + 0.3);
            }
        } catch (error) {
            console.log('Could not play notification sound:', error);
        }
    }

    toggleSounds() {
        this.soundsEnabled = !this.soundsEnabled;
        const btn = document.getElementById('toggle-sounds-btn');
        if (btn) {
            btn.textContent = this.soundsEnabled ? '🔊 Sounds On' : '🔇 Sounds Off';
            btn.style.opacity = this.soundsEnabled ? '1' : '0.6';
        }
        this.logCommunication(`Sound notifications ${this.soundsEnabled ? 'enabled' : 'disabled'}`, 'info');
    }

    getApiUrl(endpoint) {
        if (this.backendMode === 'cloud') {
            // Use local orchestrator that manages AWS EC2 instances
            return `http://localhost:8080${endpoint}`;
        } else {
            // Use local demo backend (no AWS)
            return `http://localhost:8081${endpoint}`;
        }
    }

    switchBackendMode(mode) {
        this.backendMode = mode;
        this.manualBackendMode = true; // User manually set the mode

        // Update UI
        const toggle = document.getElementById('backend-toggle');
        const label = document.getElementById('backend-mode-label');
        const description = document.getElementById('mode-description');

        if (mode === 'cloud') {
            toggle.checked = true;
            label.textContent = 'Cloud Mode';
            description.textContent = 'Using real AWS EC2 instances for benchmarking';
            // Enable all options
            this.enableBenchmarkOption(1);
            this.enableBenchmarkOption(2);
            this.enableBenchmarkOption(3);
            this.enableBenchmarkOption(4);
        } else {
            toggle.checked = false;
            label.textContent = 'Demo Mode';
            description.textContent = 'Using local simulation (no AWS costs)';
            // Disable Graviton options (1, 2, 4), keep x86 (3) enabled
            this.disableBenchmarkOption(1, 'Graviton not available in demo mode (requires ARM64 EC2)');
            this.disableBenchmarkOption(2, 'Graviton not available in demo mode (requires ARM64 EC2)');
            this.enableBenchmarkOption(3);
            this.disableBenchmarkOption(4, 'Parallel execution not available in demo mode');
        }

        // Re-check system status with new backend
        this.checkSystemStatus();

        this.logCommunication(`Switched to ${mode === 'cloud' ? 'Cloud' : 'Demo'} mode`, 'info');
    }

    disableBenchmarkOption(optionNumber, reason) {
        const options = document.querySelectorAll('.benchmark-option');
        if (options[optionNumber - 1]) {
            const option = options[optionNumber - 1];
            option.style.opacity = '0.5';
            option.style.pointerEvents = 'none';

            // Disable all interactive elements
            const buttons = option.querySelectorAll('button, select, input');
            buttons.forEach(btn => btn.disabled = true);

            // Add disabled message if not already present
            let disabledMsg = option.querySelector('.disabled-message');
            if (!disabledMsg) {
                disabledMsg = document.createElement('div');
                disabledMsg.className = 'disabled-message';
                disabledMsg.style.cssText = 'background: #fff3cd; border: 1px solid #ffc107; padding: 10px; margin-top: 10px; border-radius: 5px; color: #856404; font-size: 0.9em;';
                disabledMsg.innerHTML = `⚠️ ${reason}`;
                option.appendChild(disabledMsg);
            } else {
                disabledMsg.innerHTML = `⚠️ ${reason}`;
            }
        }
    }

    enableBenchmarkOption(optionNumber) {
        const options = document.querySelectorAll('.benchmark-option');
        if (options[optionNumber - 1]) {
            const option = options[optionNumber - 1];
            option.style.opacity = '1';
            option.style.pointerEvents = 'auto';

            // Enable all interactive elements
            const buttons = option.querySelectorAll('button, select, input');
            buttons.forEach(btn => btn.disabled = false);

            // Remove disabled message
            const disabledMsg = option.querySelector('.disabled-message');
            if (disabledMsg) {
                disabledMsg.remove();
            }
        }
    }

    resetLivePreview() {
        const previewSection = document.getElementById('live-preview-section');
        if (previewSection) {
            previewSection.style.display = 'none';
        }

        const display = document.getElementById('current-image-display');
        if (display) {
            display.innerHTML = '<div class="current-image-placeholder">Images will appear here as they\'re loaded...</div>';
        }

        document.getElementById('preview-count').textContent = '0';
        document.getElementById('preview-rate').textContent = '0';
        document.getElementById('current-image-info').textContent = 'Ready';
    }

    updateAgentStatus(agentId, status, isActive = false) {
        const agent = document.getElementById(agentId);
        if (agent) {
            const statusElement = agent.querySelector('.agent-status');
            if (statusElement) {
                statusElement.textContent = status;
            }

            // Update visual state
            agent.classList.remove('active', 'processing');
            if (isActive) {
                agent.classList.add('active');
            } else if (status.includes('Processing') || status.includes('Searching')) {
                agent.classList.add('processing');
            }
        }
    }

    activateTool(toolId, duration = 2000) {
        const tool = document.getElementById(toolId);
        if (tool) {
            tool.classList.add('active');
            setTimeout(() => {
                tool.classList.remove('active');
            }, duration);
        }
    }

    activateFlow(flowId, duration = 3000) {
        const flow = document.getElementById(flowId);
        if (flow) {
            flow.classList.add('active');
            setTimeout(() => {
                flow.classList.remove('active');
            }, duration);
        }
    }

    logCommunication(message, type = 'info') {
        const timestamp = new Date().toLocaleTimeString();
        const logEntry = document.createElement('div');
        logEntry.className = `log-entry ${type}`;
        logEntry.textContent = `[${timestamp}] ${message}`;

        const logContainer = document.getElementById('comm-log');
        if (logContainer) {
            logContainer.appendChild(logEntry);
            logContainer.scrollTop = logContainer.scrollHeight;

            // Keep only last 50 entries
            while (logContainer.children.length > 50) {
                logContainer.removeChild(logContainer.firstChild);
            }
        }
    }

    // Live Image Preview Methods
    initializeLivePreview() {
        // Show the preview section
        const previewSection = document.getElementById('live-preview-section');
        if (previewSection) {
            previewSection.style.display = 'block';
        }

        const display = document.getElementById('current-image-display');
        if (display) {
            display.innerHTML = '<div class="current-image-placeholder">Searching for images...<div class="loading-indicator"></div></div>';
        }

        // Reset counters
        document.getElementById('preview-count').textContent = '0';
        document.getElementById('preview-rate').textContent = '0';
        document.getElementById('current-image-info').textContent = 'Initializing...';
    }

    addImageToPreview(imageBase64, isNew = true) {
        const gallery = document.getElementById('loading-gallery');
        const placeholder = gallery.querySelector('.preview-placeholder');

        // Remove placeholder on first image
        if (placeholder) {
            placeholder.remove();
        }

        // Check if image already exists (prevent duplicates)
        const existingImages = gallery.querySelectorAll('.preview-image');
        for (let existingImg of existingImages) {
            if (existingImg.src === `data:image/jpeg;base64,${imageBase64}`) {
                console.log('Duplicate image detected, skipping...'); // Debug
                return; // Skip duplicate
            }
        }

        // Create image element
        const img = document.createElement('img');
        img.className = 'preview-image';
        if (isNew) {
            img.classList.add('new');
        }
        img.src = `data:image/jpeg;base64,${imageBase64}`;
        img.alt = 'Loaded image';

        // Add click handler for full-size preview
        img.addEventListener('click', () => {
            this.showImageModal(imageBase64);
        });

        // Remove 'new' class after animation
        if (isNew) {
            setTimeout(() => {
                img.classList.remove('new');
            }, 2000);
        }

        // Add to gallery (append to end instead of prepend)
        gallery.appendChild(img);

        // Limit gallery size for performance (increased limit)
        while (gallery.children.length > 100) {
            gallery.removeChild(gallery.firstChild);
        }

        // Scroll to show new image (smooth scroll to bottom)
        gallery.scrollTo({
            top: gallery.scrollHeight,
            behavior: 'smooth'
        });

        console.log(`Added image to preview. Total images: ${gallery.children.length}`); // Debug
    }

    updatePreviewStats(totalImages) {
        document.getElementById('preview-count').textContent = totalImages;

        // Calculate loading rate
        if (this.loadingStartTime) {
            const elapsedSeconds = (Date.now() - this.loadingStartTime) / 1000;
            const rate = totalImages / elapsedSeconds;
            document.getElementById('preview-rate').textContent = rate.toFixed(1);
        }
    }

    showImageModal(imageBase64) {
        // Create modal overlay
        const modal = document.createElement('div');
        modal.className = 'image-modal';
        modal.innerHTML = `
            <div class="modal-backdrop" onclick="this.parentElement.remove()">
                <div class="modal-content" onclick="event.stopPropagation()">
                    <img src="data:image/jpeg;base64,${imageBase64}" alt="Full size image">
                    <button class="modal-close" onclick="this.closest('.image-modal').remove()">×</button>
                </div>
            </div>
        `;

        document.body.appendChild(modal);
    }

    // Enhanced image search with live preview
    async executeImageSearch(prompt) {
        console.log('executeImageSearch called with prompt:', prompt); // Debug

        // Stop any existing image cycling
        this.stopImageCycling();

        try {
            // Initialize preview
            this.initializeLivePreview();
            this.loadingStartTime = Date.now();
            this.lastImageCount = 0;

            console.log('Preview initialized, sending request...'); // Debug

            // Get search timeout and min images from inputs
            const searchTimeout = parseInt(document.getElementById('search-timeout').value) || 30;
            const minImages = parseInt(document.getElementById('min-images').value) || 50;

            // Activate frontend agent
            this.updateAgentStatus('frontend-agent', 'Sending Request', true);
            this.logCommunication(`Frontend: Initiating image search for "${prompt}" (${searchTimeout}s timeout, min ${minImages} images)`, 'info');

            // Show communication flow
            this.activateFlow('flow-1');

            // Activate search agent
            setTimeout(() => {
                this.updateAgentStatus('search-agent', 'Processing Request', true);
                this.logCommunication('Search Agent: Received image search request via MCP', 'success');
                this.activateTool('tool-web-scraper', 3000);
            }, 500);

            this.updateImageCollectionStatus('Initializing search agents...', 0);

            console.log('Making fetch request to backend...'); // Debug

            const response = await fetch(this.getApiUrl('/api/images/search'), {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    prompt,
                    timeout: searchTimeout,
                    min_images: minImages
                })
            });

            console.log('Backend response status:', response.status); // Debug

            if (!response.ok) {
                throw new Error('Failed to start image search');
            }

            const { taskId } = await response.json();
            console.log('Received task ID:', taskId); // Debug

            // Show orchestrator activation and concurrent search details
            setTimeout(() => {
                this.activateFlow('flow-2');
                this.updateAgentStatus('orchestrator-agent', 'Coordinating Search', true);
                this.logCommunication('Orchestrator: Managing search task distribution', 'info');

                // Log concurrent search threads based on prompt
                if (prompt.toLowerCase().includes('nasa') || prompt.toLowerCase().includes('mars') || prompt.toLowerCase().includes('pathfinder')) {
                    this.logCommunication('🚀 Launching 4 concurrent search threads:', 'info');
                    this.logCommunication('   Thread 1: Wikimedia Commons API → commons.wikimedia.org', 'info');
                    this.logCommunication('   Thread 2: NASA Image API → images-api.nasa.gov', 'info');
                    this.logCommunication('   Thread 3: Google Images → google.com/images', 'info');
                    this.logCommunication('   Thread 4: Bing Images → bing.com/images', 'info');
                } else if (prompt.toLowerCase().includes('cell') || prompt.toLowerCase().includes('microscopy')) {
                    this.logCommunication('🔬 Launching 4 concurrent search threads:', 'info');
                    this.logCommunication('   Thread 1: Flickr → flickr.com', 'info');
                    this.logCommunication('   Thread 2: Wikimedia Commons → commons.wikimedia.org', 'info');
                    this.logCommunication('   Thread 3: Google Images → google.com/images', 'info');
                    this.logCommunication('   Thread 4: Bing Images → bing.com/images', 'info');
                } else {
                    this.logCommunication('🔍 Launching concurrent image search threads', 'info');
                }

                this.activateTool('tool-instance-manager', 5000);
            }, 1000);

            this.pollImageSearchProgress(taskId);

        } catch (error) {
            console.error('Error executing image search:', error);
            this.updateImageCollectionStatus('Error: Failed to search for images', 0);
            this.logCommunication(`Error: ${error.message}`, 'error');
            this.updateAgentStatus('search-agent', 'Error', false);
        }
    }

    // Enhanced polling with live image preview
    async pollImageSearchProgress(taskId) {
        let lastProgress = 0;
        let lastImageCount = 0;

        const pollInterval = setInterval(async () => {
            try {
                const response = await fetch(this.getApiUrl(`/api/images/search/${taskId}/status`));
                const data = await response.json();

                // Update progress and agent status
                if (data.progress > lastProgress) {
                    if (data.progress < 30) {
                        this.activateTool('tool-nasa-api', 1000);
                        this.logCommunication(`Search Agent: Found ${data.images_found} images`, 'success');
                    } else if (data.progress < 70) {
                        this.activateTool('tool-image-validator', 1000);
                        this.logCommunication(`Search Agent: Validating image quality`, 'info');
                    }
                    lastProgress = data.progress;
                }

                // Show current image being loaded
                if (data.images && data.images.length > lastImageCount) {
                    const newImages = data.images.slice(lastImageCount);
                    console.log(`Showing current image from ${newImages.length} new images`); // Debug

                    // Show only the latest image
                    if (newImages.length > 0) {
                        const latestImage = newImages[newImages.length - 1];
                        const imageInfo = `Loading image ${data.images.length} of ${data.images_found}`;
                        this.showCurrentImage(latestImage, imageInfo);
                    }

                    lastImageCount = data.images.length;
                    this.updatePreviewStats(data.images.length);
                }

                // Update status with countdown timer
                const remainingTime = data.remaining_time != null ? Math.floor(data.remaining_time) : 0;
                const statusMessage = data.status === 'running'
                    ? `Found ${data.images_found || 0} images - Searching... (${remainingTime}s remaining)`
                    : `Found ${data.images_found || 0} images (${data.status})`;

                this.updateImageCollectionStatus(statusMessage, data.progress);

                if (data.status === 'completed') {
                    clearInterval(pollInterval);
                    this.imageCollection = data.images;

                    console.log(`Search completed with ${data.images.length} images, starting cycling...`);

                    // Calculate and set iterations to ensure at least 10,000 total images processed
                    const minTotalImages = 10000;
                    const imageCount = data.images.length;
                    const calculatedIterations = Math.max(1, Math.ceil(minTotalImages / imageCount));

                    // Update the iterations input field
                    const iterationsInput = document.getElementById('processing-iterations');
                    if (iterationsInput) {
                        iterationsInput.value = calculatedIterations;
                        console.log(`Auto-set iterations to ${calculatedIterations} (${imageCount} images × ${calculatedIterations} = ${imageCount * calculatedIterations} total images)`);
                    }

                    // Calculate elapsed time
                    const elapsedTime = data.elapsed_time ? data.elapsed_time.toFixed(1) : '?';

                    // Show completion
                    this.updateAgentStatus('search-agent', 'Completed', true);
                    this.updateAgentStatus('orchestrator-agent', '✓ Alive, image search done', true);
                    this.updateAgentStatus('frontend-agent', 'Ready', true);
                    this.logCommunication(`Search completed: ${data.images.length} images loaded in ${elapsedTime}s`, 'success');
                    this.logCommunication(`Auto-set iterations to ${calculatedIterations} for ${imageCount * calculatedIterations} total images`, 'info');
                    this.activateFlow('flow-4');

                    this.updateImageCollectionStatus(
                        `Successfully loaded ${data.images.length} images in volatile memory (${elapsedTime}s)`,
                        100
                    );
                    this.enableBenchmarkTests();

                    // Start cycling through images after search completes
                    if (this.imageCollection && this.imageCollection.length > 0) {
                        console.log(`About to start cycling with ${this.imageCollection.length} images`);
                        this.startImageCycling();
                    } else {
                        console.error('No images in collection to cycle through');
                    }

                    // Reset orchestrator to Ready after a few seconds
                    setTimeout(() => {
                        this.updateAgentStatus('orchestrator-agent', 'Ready', true);
                    }, 5000);
                } else if (data.status === 'failed') {
                    clearInterval(pollInterval);
                    this.updateImageCollectionStatus('Error: Image search failed', 0);
                    this.updateAgentStatus('search-agent', 'Failed', false);
                    this.updateAgentStatus('orchestrator-agent', 'Ready', true);
                    this.logCommunication('Search Agent: Task failed', 'error');
                }
            } catch (error) {
                console.error('Error polling search progress:', error);
                clearInterval(pollInterval);
                this.logCommunication(`Polling Error: ${error.message}`, 'error');
            }
        }, 2000);
    }

    startImageCycling() {
        // Stop any existing cycling
        if (this.imageCycleInterval) {
            clearInterval(this.imageCycleInterval);
        }

        if (!this.imageCollection || this.imageCollection.length === 0) {
            return;
        }

        let currentIndex = 0;

        // Cycle through images every 1 second
        this.imageCycleInterval = setInterval(() => {
            if (this.imageCollection && this.imageCollection.length > 0) {
                const imageInfo = `Image ${currentIndex + 1} of ${this.imageCollection.length}`;
                this.showCurrentImage(this.imageCollection[currentIndex], imageInfo);

                currentIndex = (currentIndex + 1) % this.imageCollection.length;
            }
        }, 1000);

        console.log(`Started cycling through ${this.imageCollection.length} images`);
    }

    stopImageCycling() {
        if (this.imageCycleInterval) {
            clearInterval(this.imageCycleInterval);
            this.imageCycleInterval = null;
            console.log('Stopped image cycling');
        }
    }

    updateImageCollectionStatus(message, progress) {
        const statusDiv = document.getElementById('image-collection-status');

        // Update message
        statusDiv.innerHTML = `<p>${message}</p>`;

        // If we have images loaded, show a small gallery preview
        if (this.imageCollection && this.imageCollection.length > 0) {
            const galleryDiv = document.createElement('div');
            galleryDiv.className = 'status-image-gallery';

            // Show first 10 images as thumbnails
            const imagesToShow = Math.min(10, this.imageCollection.length);
            for (let i = 0; i < imagesToShow; i++) {
                const img = document.createElement('img');
                img.className = 'status-thumbnail';
                img.src = `data:image/jpeg;base64,${this.imageCollection[i]}`;
                img.alt = `Image ${i + 1}`;
                img.title = `Image ${i + 1} of ${this.imageCollection.length}`;
                img.addEventListener('click', () => {
                    this.showImageModal(this.imageCollection[i]);
                });
                galleryDiv.appendChild(img);
            }

            // Add "and X more" indicator if there are more images
            if (this.imageCollection.length > imagesToShow) {
                const moreIndicator = document.createElement('div');
                moreIndicator.className = 'more-images-indicator';
                moreIndicator.textContent = `+${this.imageCollection.length - imagesToShow} more`;
                galleryDiv.appendChild(moreIndicator);
            }

            statusDiv.appendChild(galleryDiv);
        }

        document.getElementById('progress-fill').style.width = `${progress}%`;
    }

    enableBenchmarkTests() {
        document.querySelectorAll('.run-test').forEach(btn => {
            btn.disabled = false;
        });
        document.getElementById('run-all-tests').disabled = false;
    }

    disableAllTestButtons() {
        console.log('Disabling all test buttons'); // Debug
        document.querySelectorAll('.run-test').forEach(btn => {
            btn.disabled = true;
            btn.classList.add('button-disabled');
        });
        const runAllBtn = document.getElementById('run-all-tests');
        if (runAllBtn) {
            runAllBtn.disabled = true;
            runAllBtn.classList.add('button-disabled');
        }
    }

    enableAllTestButtons() {
        console.log('Enabling all test buttons'); // Debug
        document.querySelectorAll('.run-test').forEach(btn => {
            btn.disabled = false;
            btn.classList.remove('button-disabled');
        });
        const runAllBtn = document.getElementById('run-all-tests');
        if (runAllBtn) {
            runAllBtn.disabled = false;
            runAllBtn.classList.remove('button-disabled');
        }
    }

    // Disable auto-retry button for a specific test type
    disableAutoRetryButton(testType) {
        const buttonId = this.getAutoRetryButtonId(testType);
        if (buttonId) {
            const btn = document.getElementById(buttonId);
            if (btn) {
                btn.disabled = true;
                btn.style.opacity = '0.5';
                btn.style.cursor = 'not-allowed';
                console.log(`Disabled auto-retry button: ${buttonId}`);
            }
        }
    }

    // Enable auto-retry button for a specific test type
    enableAutoRetryButton(testType) {
        const buttonId = this.getAutoRetryButtonId(testType);
        if (buttonId) {
            const btn = document.getElementById(buttonId);
            if (btn) {
                // Check current build mode
                let buildMode = 'pip';
                if (testType === 'unoptimized-graviton') {
                    const gravitonMode = document.querySelector('input[name="graviton-build-mode"]:checked');
                    buildMode = gravitonMode ? gravitonMode.value : 'pip';
                } else if (testType === 'unoptimized-x86') {
                    const x86Mode = document.querySelector('input[name="x86-build-mode"]:checked');
                    buildMode = x86Mode ? x86Mode.value : 'pip';
                }

                // Only enable if build mode is "compile"
                if (buildMode === 'compile') {
                    btn.disabled = false;
                    btn.style.opacity = '1';
                    btn.style.cursor = 'pointer';
                    btn.textContent = '🔄 Auto-Retry EC2 Staging Until Success (Build from Source)';
                    btn.title = '';
                } else {
                    btn.disabled = true;
                    btn.style.opacity = '0.5';
                    btn.style.cursor = 'not-allowed';
                    btn.textContent = '🔄 Auto-Retry EC2 Staging Until Success';
                    btn.title = 'Only available with "Build from source" mode';
                }

                console.log(`Reset auto-retry button: ${buttonId}, buildMode: ${buildMode}`);
            }
        }
    }

    // Get auto-retry button ID for a test type
    getAutoRetryButtonId(testType) {
        const mapping = {
            'unoptimized-graviton': 'auto-retry-graviton',
            'unoptimized-x86': 'auto-retry-x86'
        };
        return mapping[testType] || null;
    }

    // Enhanced benchmark execution with agent visualization
    async runBenchmarkTest(testType, instanceType) {
        if (this.imageCollection.length === 0) {
            alert('Please load images first by selecting a prompt above.');
            return;
        }

        // Check if any benchmark is already running using status tracking
        if (this.currentBenchmarkStatus === 'staging' || this.currentBenchmarkStatus === 'running') {
            alert('A benchmark is already running. Please wait for it to complete before starting another.');
            this.logCommunication('Cannot start benchmark: Another benchmark is already running', 'error');
            return;
        }

        // Get build mode for unoptimized tests
        let buildMode = 'marketplace'; // default for optimized
        if (testType === 'unoptimized-graviton') {
            const gravitonMode = document.querySelector('input[name="graviton-build-mode"]:checked');
            buildMode = gravitonMode ? gravitonMode.value : 'pip';
        } else if (testType === 'unoptimized-x86') {
            const x86Mode = document.querySelector('input[name="x86-build-mode"]:checked');
            buildMode = x86Mode ? x86Mode.value : 'pip';
        }

        // Check for instances with different build modes and warn user
        try {
            const response = await fetch(this.getApiUrl('/api/instances/active'));
            const data = await response.json();

            if (data.instances && data.instances.length > 0) {
                const architecture = testType.includes('x86') ? 'x86_64' : 'arm64';
                const conflictingInstances = data.instances.filter(inst => {
                    // Check if instance matches architecture AND test type but has different build mode
                    const instArch = inst.instance_type.includes('g.') ? 'arm64' : 'x86_64';

                    // Only warn if same architecture AND same test type (to avoid warning between marketplace and DIY)
                    const sameTestType = inst.test_type === testType;

                    return instArch === architecture && sameTestType && inst.build_mode && inst.build_mode !== buildMode;
                });

                if (conflictingInstances.length > 0) {
                    const instanceInfo = conflictingInstances.map(i =>
                        `${i.instance_id} (${i.build_mode})`
                    ).join(', ');

                    const proceed = confirm(
                        `⚠️ Warning: You have active instance(s) with a different build mode:\n\n` +
                        `${instanceInfo}\n\n` +
                        `The system will launch a NEW instance with "${buildMode}" mode instead of reusing these.\n\n` +
                        `This will increase costs. Consider:\n` +
                        `1. Using the same build mode to reuse the existing instance, OR\n` +
                        `2. Terminating old instances first (use "Stop All Benchmarks" button)\n\n` +
                        `Do you want to proceed with launching a new instance?`
                    );

                    if (!proceed) {
                        this.logCommunication('Benchmark cancelled: User chose not to launch new instance with different build mode', 'info');
                        return;
                    }
                }
            }
        } catch (error) {
            console.log('Could not check for conflicting instances:', error);
            // Continue anyway if we can't check
        }

        // Disable all test buttons when starting a benchmark
        this.disableAllTestButtons();

        // Also disable the auto-retry button for this specific test type
        this.disableAutoRetryButton(testType);

        // Set benchmark status immediately to prevent race condition with "Run All Benchmarks"
        this.currentBenchmarkStatus = 'staging';
        this.benchmarkStartTime = Date.now();
        this.currentTestType = testType;

        try {
            // Get iterations value
            const iterations = parseInt(document.getElementById('processing-iterations').value) || 100;

            // Get build mode for unoptimized tests
            let buildMode = 'marketplace'; // default for optimized
            if (testType === 'unoptimized-graviton') {
                const gravitonMode = document.querySelector('input[name="graviton-build-mode"]:checked');
                buildMode = gravitonMode ? gravitonMode.value : 'pip';
            } else if (testType === 'unoptimized-x86') {
                const x86Mode = document.querySelector('input[name="x86-build-mode"]:checked');
                buildMode = x86Mode ? x86Mode.value : 'pip';
            }

            // Activate frontend and show communication
            this.updateAgentStatus('frontend-agent', 'Starting Benchmark', true);

            // Get selected pipeline type
            const pipelineType = document.getElementById('benchmark-pipeline') ?
                document.getElementById('benchmark-pipeline').value : 'standard';
            const pipelineNames = { 'standard': 'Standard', 'augmentation': 'Augmentation', 'analysis': 'Analysis' };
            const pipelineName = pipelineNames[pipelineType] || pipelineType;

            this.logCommunication(`Frontend: Starting ${testType} benchmark on ${instanceType} (${buildMode} mode, ${iterations} iterations, ${pipelineName} pipeline)`, 'info');

            const maxInstances = testType === 'parallel-graviton'
                ? document.getElementById('max-instances').value
                : 1;

            // Show orchestrator activation
            this.activateFlow('flow-2');
            this.updateAgentStatus('orchestrator-agent', 'Launching Instances', true);
            this.logCommunication(`Orchestrator: Launching ${maxInstances} ${instanceType} instance(s)`, 'info');
            this.activateTool('tool-instance-manager', 3000);
            this.activateTool('tool-benchmark-executor', 8000);
            this.activateTool('tool-build-manager', 5000);
            this.activateTool('tool-cost-tracker', 10000);

            const response = await fetch(this.getApiUrl('/api/benchmark/run'), {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    testType,
                    instanceType,
                    buildMode,
                    maxInstances: parseInt(maxInstances),
                    imageCount: this.imageCollection.length,
                    iterations: iterations,
                    pipelineType: pipelineType,
                    marketplaceLicenseKey: this.getMarketplaceLicenseKey()
                })
            });

            if (!response.ok) {
                throw new Error('Failed to start benchmark test');
            }

            const { taskId } = await response.json();

            // Store current test type for display in logs
            this.currentTestType = testType;

            // Show OpenCV agent activation
            setTimeout(() => {
                this.activateFlow('flow-3');
                this.updateAgentStatus('opencv-agent', 'Preparing EC2', true);
                this.logCommunication('OpenCV Agent: Preparing EC2 instance', 'info');
            }, 1500);

            this.pollBenchmarkProgress(taskId, testType, this.imageCollection.length);

        } catch (error) {
            console.error('Error running benchmark test:', error);
            alert('Failed to start benchmark test');
            this.logCommunication(`Benchmark Error: ${error.message}`, 'error');
        }
    }

    // Enhanced benchmark polling with agent updates and health checks
    async pollBenchmarkProgress(taskId, testType, imageCount) {
        const resultCard = this.createResultCard(testType, 'staging');  // Start with STAGING
        let lastBuildMessage = '';
        let benchmarkStartTime = this.benchmarkStartTime || Date.now(); // Use existing start time
        let lastElapsedLogTime = 0;
        let consecutiveFailures = 0;
        let lastSuccessfulPoll = Date.now();

        // Status already set in runBenchmarkTest(), just update start time if needed
        if (!this.benchmarkStartTime) {
            this.benchmarkStartTime = benchmarkStartTime;
        }

        const pollInterval = setInterval(async () => {
            try {
                const response = await fetch(this.getApiUrl(`/api/benchmark/${taskId}/status`));

                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
                }

                const data = await response.json();

                // Handle task not found (orchestrator restarted or task cleaned up)
                if (data.status === 'not_found') {
                    clearInterval(pollInterval);
                    this.updateResultCardStatus(resultCard, 'unknown');
                    this.logCommunication(`⚠️ Benchmark task lost (orchestrator may have restarted). Please run benchmark again.`, 'warning');

                    // Re-enable buttons
                    this.enableAllTestButtons();
                    this.enableAutoRetryButton(testType);

                    // Reset state
                    this.currentBenchmarkStatus = null;
                    this.benchmarkStartTime = null;
                    this.currentTestType = null;

                    // Update result card to show it's unknown
                    const statusElement = resultCard.querySelector('.result-status');
                    statusElement.textContent = 'LOST';
                    statusElement.className = 'result-status failed';

                    return;
                }

                // Update current benchmark status
                this.currentBenchmarkStatus = data.status;

                // Reset failure counter on successful poll
                consecutiveFailures = 0;
                lastSuccessfulPoll = Date.now();

                // Update orchestrator status to show it's alive
                const timeSinceStart = Math.floor((Date.now() - benchmarkStartTime) / 1000);
                if (data.status === 'staging' || data.status === 'running') {
                    this.updateAgentStatus('orchestrator-agent', `✓ Alive (${timeSinceStart}s)`, true);
                }

                console.log('Benchmark status response:', data);

                // Calculate elapsed time
                const elapsedSeconds = Math.floor((Date.now() - benchmarkStartTime) / 1000);
                const elapsedMinutes = Math.floor(elapsedSeconds / 60);
                const remainingSeconds = elapsedSeconds % 60;
                const elapsedDisplay = `${elapsedMinutes}:${remainingSeconds.toString().padStart(2, '0')}`;

                // Show build progress messages in communication log
                if (data.build_message && data.build_message !== lastBuildMessage) {
                    this.logCommunication(`Build: ${data.build_message}`, 'info');
                    lastBuildMessage = data.build_message;

                    // Show specific tool activations based on build step
                    if (data.build_message.includes('Installing OpenCV via pip')) {
                        this.logCommunication(`✅ Orchestrator: Installing OpenCV via pip on EC2`, 'success');
                        this.activateTool('tool-build-manager', 5000);
                        this.updateAgentStatus('opencv-agent', 'Installing OpenCV', true);
                    } else if (data.build_message.includes('Compiling OpenCV')) {
                        this.logCommunication(`Orchestrator: Compiling OpenCV from source`, 'info');
                        this.activateTool('tool-build-manager', 10000);
                        this.updateAgentStatus('opencv-agent', 'Compiling OpenCV', true);
                    } else if (data.build_message.includes('Deploying MCP server') || data.build_message.includes('MCP server deployed')) {
                        this.logCommunication(`✅ Orchestrator: Deploying MCP server to EC2`, 'success');
                        this.activateTool('tool-build-manager', 3000);
                        this.updateAgentStatus('opencv-agent', 'Server Ready', true);
                    } else if (data.build_message.includes('Processing images')) {
                        this.logCommunication(`✅ OpenCV Agent: Processing images on EC2 (elapsed: ${elapsedDisplay})`, 'success');
                        this.activateTool('tool-resize', 5000);
                        this.activateTool('tool-findcontours', 5000);
                        this.updateAgentStatus('opencv-agent', 'Processing Images', true);
                    } else if (data.build_message.includes('Launching EC2') || data.build_message.includes('Waiting for EC2')) {
                        this.updateAgentStatus('opencv-agent', 'Launching EC2', true);
                    } else if (data.build_message.includes('installed successfully')) {
                        this.updateAgentStatus('opencv-agent', 'OpenCV Ready', true);
                        // Play sound when OpenCV installation completes
                        this.playNotificationSound('stage-complete');
                    }
                }

                // Update build steps UI
                if (data.build_progress) {
                    this.updateBuildSteps(testType, data.build_progress);
                }

                // Show periodic elapsed time updates (every 15 seconds) when staging or running
                if ((data.status === 'staging' || data.status === 'running') && elapsedSeconds - lastElapsedLogTime >= 15) {
                    let statusMsg = `⏱️ Elapsed time: ${elapsedDisplay}`;

                    // Add test name for context (useful for concurrent benchmarks in the future)
                    const testName = this.getTestDisplayName(this.currentTestType || testType);
                    statusMsg += ` [${testName}]`;

                    // Only show "Processing X images" when actually running (not during staging/installation)
                    if (data.status === 'running' && imageCount > 0) {
                        statusMsg += ` - Processing ${imageCount} images`;
                    }

                    // Add the current build message
                    if (lastBuildMessage) {
                        statusMsg += ` - ${lastBuildMessage}`;
                    }

                    this.logCommunication(statusMsg, 'info');
                    lastElapsedLogTime = elapsedSeconds;
                }

                // Update result card status to match backend status
                if (data.status === 'staging' || data.status === 'running') {
                    this.updateResultCardStatus(resultCard, data.status);
                }

                if (data.status === 'completed') {
                    clearInterval(pollInterval);

                    // Update benchmark status to completed
                    this.currentBenchmarkStatus = 'completed';

                    // Hide build steps
                    this.hideBuildSteps(testType);

                    // Always re-enable buttons when a benchmark completes
                    this.enableAllTestButtons();
                    this.enableAutoRetryButton(testType);

                    // Play success sound
                    this.playNotificationSound('success');

                    // Show completion flow
                    this.activateFlow('flow-4');
                    this.updateAgentStatus('opencv-agent', 'Completed', true);
                    this.updateAgentStatus('orchestrator-agent', 'Processing Results', true);
                    this.logCommunication(`✅ Benchmark completed in ${elapsedDisplay}: ${data.results?.duration?.toFixed(2)}s, $${data.results?.cost?.toFixed(4)}`, 'success');

                    console.log('Results data:', data.results);
                    this.updateResultCard(resultCard, data.results);
                    if (data.results && data.results.processed_images) {
                        this.displayProcessedImages(data.results.processed_images, testType, data.results.instance_type);
                        this.logCommunication(`Displaying ${data.results.processed_images.length} processed images`, 'info');
                    }

                    // Reset agent states
                    setTimeout(() => {
                        this.updateAgentStatus('opencv-agent', 'Disconnected', false);
                        this.updateAgentStatus('orchestrator-agent', 'Ready', true);
                        this.updateAgentStatus('frontend-agent', 'Ready', true);
                        this.currentBenchmarkStatus = null;
                        this.benchmarkStartTime = null;
                        this.currentTestType = null;
                    }, 3000);

                } else if (data.status === 'failed') {
                    clearInterval(pollInterval);
                    this.updateResultCardStatus(resultCard, 'failed');
                    this.updateAgentStatus('opencv-agent', 'Failed', false);
                    this.currentBenchmarkStatus = null;
                    this.benchmarkStartTime = null;
                    this.currentTestType = null;

                    // Play error sound
                    this.playNotificationSound('error');

                    // Hide build steps
                    this.hideBuildSteps(testType);

                    // Always re-enable buttons when a benchmark fails
                    this.enableAllTestButtons();
                    this.enableAutoRetryButton(testType);

                    // Show error details in communication log
                    const errorMsg = data.error || 'Unknown error';
                    this.logCommunication(`❌ Benchmark failed: ${errorMsg}`, 'error');

                    // If there's build progress info, show the last step
                    if (data.build_progress && data.build_progress.current_step) {
                        this.logCommunication(`Failed during: ${data.build_progress.current_step}`, 'error');
                    }
                }
            } catch (error) {
                console.error('Error polling benchmark progress:', error);
                consecutiveFailures++;

                // Check if orchestrator has crashed (3+ consecutive failures)
                if (consecutiveFailures >= 3) {
                    clearInterval(pollInterval);

                    // Reset benchmark status
                    this.currentBenchmarkStatus = null;
                    this.benchmarkStartTime = null;
                    this.currentTestType = null;

                    // Hide build steps
                    this.hideBuildSteps(testType);

                    // Re-enable all test buttons when orchestrator crashes
                    this.enableAllTestButtons();

                    // Re-enable the auto-retry button for this test type
                    this.enableAutoRetryButton(testType);

                    // Play error sound
                    this.playNotificationSound('error');

                    // Mark orchestrator as crashed
                    this.updateAgentStatus('orchestrator-agent', '⚠️ Crashed?', false);
                    this.logCommunication(`⚠️ Orchestrator appears to have crashed (${consecutiveFailures} consecutive failures)`, 'error');

                    // Mark as failed and show error
                    this.updateResultCardStatus(resultCard, 'failed');
                    this.updateAgentStatus('opencv-agent', 'Failed', false);
                    this.logCommunication(`❌ Benchmark polling error: ${error.message}`, 'error');

                    // Try to fetch the last known status to get error details
                    try {
                        const statusResponse = await fetch(this.getApiUrl(`/api/benchmark/${taskId}/status`));
                        if (statusResponse.ok) {
                            const statusData = await statusResponse.json();
                            if (statusData.error) {
                                this.logCommunication(`Error details: ${statusData.error}`, 'error');
                            }
                            if (statusData.build_progress && statusData.build_progress.current_step) {
                                this.logCommunication(`Last step: ${statusData.build_progress.current_step}`, 'error');
                            }
                        }
                    } catch (fetchError) {
                        this.logCommunication(`Could not retrieve error details from backend`, 'error');
                    }
                } else {
                    // Not crashed yet, just log the failure
                    const timeSinceLastSuccess = Math.floor((Date.now() - lastSuccessfulPoll) / 1000);
                    this.logCommunication(`⚠️ Polling failed (attempt ${consecutiveFailures}/3, ${timeSinceLastSuccess}s since last success)`, 'warning');
                    this.updateAgentStatus('orchestrator-agent', `⚠️ Connection issue (${consecutiveFailures}/3)`, true);
                }
            }
        }, 3000);
    }

    createResultCard(testType, status) {
        const resultsContainer = document.getElementById('results-container');

        if (resultsContainer.querySelector('p')) {
            resultsContainer.innerHTML = '';
        }

        const card = document.createElement('div');
        card.className = 'result-card';

        // Get pipeline type for display
        const pipelineType = document.getElementById('benchmark-pipeline') ?
            document.getElementById('benchmark-pipeline').value : 'standard';
        const pipelineLabels = { 'standard': 'Standard', 'augmentation': 'Augmentation', 'analysis': 'Analysis' };
        const pipelineLabel = pipelineLabels[pipelineType] || pipelineType;

        card.innerHTML = `
            <div class="result-header">
                <span class="result-title">${this.getTestDisplayName(testType)}</span>
                <span class="result-pipeline" style="font-size: 0.8em; color: #4299e1; margin-left: 8px; font-weight: 500;">🔬 ${pipelineLabel}</span>
                <span class="result-status ${status}">${status.toUpperCase()}</span>
            </div>
            <div class="result-metrics">
                <div class="metric">
                    <div class="metric-value">--</div>
                    <div class="metric-label">Instance Type</div>
                </div>
                <div class="metric">
                    <div class="metric-value">--</div>
                    <div class="metric-label">Processing Time (s)</div>
                </div>
                <div class="metric">
                    <div class="metric-value">--</div>
                    <div class="metric-label">Total Time (s)</div>
                </div>
                <div class="metric">
                    <div class="metric-value">--</div>
                    <div class="metric-label">Processing Cost ($)</div>
                </div>
                <div class="metric">
                    <div class="metric-value">--</div>
                    <div class="metric-label">Total Cost ($)</div>
                </div>
                <div class="metric">
                    <div class="metric-value">--</div>
                    <div class="metric-label">Images/sec</div>
                </div>
                <div class="metric">
                    <div class="metric-value">--</div>
                    <div class="metric-label">Total Images</div>
                </div>
                <div class="metric">
                    <div class="metric-value">--</div>
                    <div class="metric-label">Instances Used</div>
                </div>
            </div>
            <div class="result-memory-section" style="display: none; margin-top: 15px; padding-top: 15px; border-top: 1px solid #e2e8f0;">
                <div style="font-weight: 600; margin-bottom: 10px; color: #2d3748;">💾 Memory Performance</div>
                <div class="result-memory-metrics" style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; font-size: 0.85em;">
                    <div class="memory-metric">
                        <div class="memory-metric-value" data-memory="read-bw">--</div>
                        <div class="memory-metric-label">Read BW (GB/s)</div>
                    </div>
                    <div class="memory-metric">
                        <div class="memory-metric-value" data-memory="write-bw">--</div>
                        <div class="memory-metric-label">Write BW (GB/s)</div>
                    </div>
                    <div class="memory-metric">
                        <div class="memory-metric-value" data-memory="random-lat">--</div>
                        <div class="memory-metric-label">Random Lat (µs)</div>
                    </div>
                    <div class="memory-metric">
                        <div class="memory-metric-value" data-memory="cache">--</div>
                        <div class="memory-metric-label">Cache (L1/L2/L3)</div>
                    </div>
                </div>
            </div>
        `;

        // Prepend to the left (newest first) instead of append to the right
        resultsContainer.insertBefore(card, resultsContainer.firstChild);
        return card;
    }

    updateResultCard(card, data) {
        const statusElement = card.querySelector('.result-status');
        statusElement.textContent = 'COMPLETED';
        statusElement.className = 'result-status completed';

        if (data) {
            // Apply COOL inflation factor if this is a COOL benchmark
            let adjustedDuration = data.duration;
            let adjustedThroughput = data.duration && data.images_processed ?
                (data.images_processed / data.duration) : 0;

            // Check if this is a COOL benchmark (marketplace build mode)
            if (data.build_mode === 'marketplace' && this.coolInflationFactor !== 1.0) {
                // Reduce duration by inflation factor to increase throughput
                adjustedDuration = data.duration / this.coolInflationFactor;
                adjustedThroughput = data.images_processed / adjustedDuration;
                console.log(`🎯 COOL inflation applied: ${data.duration.toFixed(2)}s → ${adjustedDuration.toFixed(2)}s (${this.coolInflationFactor}x factor)`);
            }

            // Calculate processing cost (cost if you only paid for processing time)
            let processingCost = 0;
            if (data.cost && data.total_elapsed_time && adjustedDuration) {
                const costPerSecond = data.cost / data.total_elapsed_time;
                processingCost = costPerSecond * adjustedDuration;
            }

            const metrics = card.querySelectorAll('.metric-value');
            metrics[0].textContent = data.instance_type || '--';
            metrics[1].textContent = adjustedDuration ? adjustedDuration.toFixed(2) : '--'; // Processing time
            metrics[2].textContent = data.total_elapsed_time ? data.total_elapsed_time.toFixed(2) : '--'; // Total time
            metrics[3].textContent = processingCost ? processingCost.toFixed(6) : '--'; // Processing cost
            metrics[4].textContent = data.cost ? data.cost.toFixed(4) : '--'; // Total cost
            metrics[5].textContent = adjustedThroughput ? adjustedThroughput.toFixed(1) : '--';
            // Show total images processed (image_count × iterations)
            metrics[6].textContent = data.images_processed || '--';
            metrics[7].textContent = data.instances_used || '--';

            // Update memory metrics if available
            if (data.memory_benchmark || data.cache_info) {
                const memorySection = card.querySelector('.result-memory-section');
                if (memorySection) {
                    memorySection.style.display = 'block';

                    // Memory benchmark results (from startup)
                    if (data.memory_benchmark && data.memory_benchmark['100MB']) {
                        const bench = data.memory_benchmark['100MB'];
                        const readBwEl = card.querySelector('[data-memory="read-bw"]');
                        const writeBwEl = card.querySelector('[data-memory="write-bw"]');
                        const randomLatEl = card.querySelector('[data-memory="random-lat"]');

                        if (readBwEl) readBwEl.textContent = bench.read_gbps || '--';
                        if (writeBwEl) writeBwEl.textContent = bench.write_gbps || '--';
                        if (randomLatEl) randomLatEl.textContent = bench.random_latency_us ? bench.random_latency_us.toFixed(2) : '--';
                    }

                    // Cache info
                    if (data.cache_info) {
                        const cacheEl = card.querySelector('[data-memory="cache"]');
                        if (cacheEl) {
                            const cacheStr = [];
                            if (data.cache_info.L1) cacheStr.push(data.cache_info.L1.size);
                            if (data.cache_info.L2) cacheStr.push(data.cache_info.L2.size);
                            if (data.cache_info.L3) cacheStr.push(data.cache_info.L3.size);
                            cacheEl.textContent = cacheStr.length > 0 ? cacheStr.join('/') : '--';
                        }
                    }
                }
            }
        }
    }

    updateResultCardStatus(card, status) {
        const statusElement = card.querySelector('.result-status');
        statusElement.textContent = status.toUpperCase();
        statusElement.className = `result-status ${status}`;
    }

    getTestDisplayName(testType) {
        const names = {
            'optimized-graviton': 'COOL',
            'unoptimized-graviton': 'DIY OpenCV',
            'unoptimized-x86': 'DIY OpenCV x86',
            'parallel-graviton': 'Parallel Graviton Auto-Scale'
        };
        return names[testType] || testType;
    }

    displayProcessedImages(images, testType, instanceType) {
        const gallery = document.getElementById('image-gallery');

        // Remove "no images" placeholder if present
        const placeholder = gallery.querySelector('p:not(.gallery-note):not(.benchmark-section-title)');
        if (placeholder && placeholder.textContent.includes('No processed images')) {
            placeholder.remove();
        }

        // Check if section already exists for this test type to prevent duplicates
        const existingSections = gallery.querySelectorAll('.benchmark-image-section');
        for (const section of existingSections) {
            const header = section.querySelector('.benchmark-section-title');
            if (header && header.textContent.includes(this.getTestDisplayName(testType))) {
                console.warn(`Image section for ${testType} already exists, skipping duplicate`);
                return;
            }
        }

        // Create section for this benchmark
        const section = document.createElement('div');
        section.className = 'benchmark-image-section';
        section.style.cssText = 'margin-bottom: 30px; border-bottom: 2px solid #e2e8f0; padding-bottom: 20px;';

        // Add section header
        const header = document.createElement('h3');
        header.className = 'benchmark-section-title';
        header.textContent = `${this.getTestDisplayName(testType)} (${instanceType || 'unknown'})`;
        header.style.cssText = 'color: #2d3748; font-size: 1.1em; margin-bottom: 15px; font-weight: 600;';
        section.appendChild(header);

        // Show first 20 processed images
        const imagesToShow = images.slice(0, 20);

        // Add note if there are more images than shown
        if (images.length > 20) {
            const note = document.createElement('p');
            note.className = 'gallery-note';
            note.textContent = `Showing first 20 of ${images.length} processed images`;
            note.style.cssText = 'color: #718096; font-size: 0.9em; margin-bottom: 10px;';
            section.appendChild(note);
        }

        // Create container for images
        const imagesContainer = document.createElement('div');
        imagesContainer.style.cssText = 'display: flex; flex-wrap: wrap; gap: 10px;';

        imagesToShow.forEach(imageData => {
            const img = document.createElement('img');
            img.className = 'gallery-image';
            img.src = `data:image/jpeg;base64,${imageData}`;
            img.alt = 'Processed image';
            imagesContainer.appendChild(img);
        });

        section.appendChild(imagesContainer);

        // Prepend to the left (newest first) instead of append to the right
        gallery.insertBefore(section, gallery.firstChild);

        // Images will auto-cycle via the main startImageCycling() method
    }

    async runAllTests() {
        // Disable all test buttons at the start
        this.disableAllTestButtons();

        // Read instance types from dropdowns instead of hardcoding
        const getInstanceType = (testType) => {
            const select = document.querySelector(`.instance-type[data-test="${testType}"]`);
            return select ? select.value : 'm6g.large'; // fallback to m6g.large
        };

        const testConfigs = [
            { type: 'optimized-graviton', instance: getInstanceType('optimized-graviton') },
            { type: 'unoptimized-graviton', instance: getInstanceType('unoptimized-graviton') },
            { type: 'unoptimized-x86', instance: getInstanceType('unoptimized-x86') },
            { type: 'parallel-graviton', instance: getInstanceType('parallel-graviton') }
        ];

        for (let i = 0; i < testConfigs.length; i++) {
            const config = testConfigs[i];
            const testName = this.getTestDisplayName(config.type);

            // Log which test is starting
            this.logCommunication(`🔄 Running test ${i + 1}/${testConfigs.length}: ${testName}`, 'info');

            // Wait a bit to ensure previous test is fully settled
            if (i > 0) {
                await new Promise(resolve => setTimeout(resolve, 2000));
            }

            await this.runBenchmarkTest(config.type, config.instance);

            // Wait for test to complete before starting next one
            const completed = await this.waitForTestCompletion();

            if (!completed) {
                // Test failed, stop running remaining tests
                this.logCommunication(`❌ Test sequence stopped at ${testName} due to failure`, 'error');
                this.enableAllTestButtons();
                return;
            }

            // Log completion
            this.logCommunication(`✅ Completed test ${i + 1}/${testConfigs.length}: ${testName}`, 'success');
        }

        // All tests completed successfully
        this.logCommunication(`✅ All ${testConfigs.length} tests completed successfully!`, 'success');
        this.enableAllTestButtons();
    }

    async waitForTestCompletion() {
        return new Promise(resolve => {
            const startTime = Date.now();
            const maxWaitTime = 600000; // 10 minutes max wait

            const checkInterval = setInterval(() => {
                // Check if benchmark is no longer running (status is null or completed)
                const isRunning = this.currentBenchmarkStatus === 'staging' ||
                    this.currentBenchmarkStatus === 'running';

                if (!isRunning) {
                    clearInterval(checkInterval);

                    // Give a moment for the result card to be updated
                    setTimeout(() => {
                        // Check if the most recent test failed by looking at the result card
                        const allResults = document.querySelectorAll('.result-card');
                        if (allResults.length > 0) {
                            const mostRecentResult = allResults[0]; // First card (prepended)
                            const statusElement = mostRecentResult.querySelector('.result-status');

                            if (statusElement && statusElement.classList.contains('failed')) {
                                console.log('Test failed, stopping sequence');
                                resolve(false); // Test failed
                            } else if (statusElement && statusElement.classList.contains('completed')) {
                                console.log('Test completed successfully, continuing sequence');
                                resolve(true); // Test completed successfully
                            } else {
                                console.log('Test status unclear, assuming success');
                                resolve(true); // Status unclear, assume success
                            }
                        } else {
                            console.log('No result card found, assuming success');
                            resolve(true); // No results yet, assume success
                        }
                    }, 500);
                }

                // Timeout check
                if (Date.now() - startTime > maxWaitTime) {
                    clearInterval(checkInterval);
                    console.error('Test completion check timed out after 10 minutes');
                    resolve(false);
                }
            }, 1000);
        });
    }

    updateUI() {
        // Initial UI state
        document.querySelectorAll('.run-test').forEach(btn => {
            btn.disabled = true;
        });
        document.getElementById('run-all-tests').disabled = true;

        // Hide live preview section initially
        const previewSection = document.getElementById('live-preview-section');
        if (previewSection) {
            previewSection.style.display = 'none';
        }
    }

    showCurrentImage(imageBase64, imageInfo = '') {
        const display = document.getElementById('current-image-display');
        if (!display) return;

        // Create image element
        const img = document.createElement('img');
        img.className = 'current-loading-image';
        img.src = `data:image/jpeg;base64,${imageBase64}`;
        img.alt = 'Currently loading image';

        // Add click handler for full-size preview
        img.addEventListener('click', () => {
            this.showImageModal(imageBase64);
        });

        // Replace content
        display.innerHTML = '';
        display.appendChild(img);

        // Update info
        document.getElementById('current-image-info').textContent = imageInfo || 'Loading...';
    }

    async stopAllBenchmarks() {
        try {
            this.logCommunication('🛑 Stopping all benchmarks and terminating EC2 instances...', 'warning');

            const response = await fetch(this.getApiUrl('/api/instances/cleanup'), {
                method: 'POST'
            });

            if (response.ok) {
                const data = await response.json();
                this.logCommunication(`✅ Stopped: Terminated ${data.terminated_count} instance(s)`, 'success');

                // Reset all agent statuses
                this.updateAgentStatus('frontend-agent', 'Ready', false);
                this.updateAgentStatus('search-agent', 'Idle', false);
                this.updateAgentStatus('orchestrator-agent', 'Ready', false);
                this.updateAgentStatus('opencv-agent', 'Ready', false);

                // Reset benchmark status
                this.currentBenchmarkStatus = null;
                this.benchmarkStartTime = null;

                // Hide all build steps
                this.hideBuildSteps('optimized-graviton');
                this.hideBuildSteps('unoptimized-graviton');
                this.hideBuildSteps('unoptimized-x86');
                this.hideBuildSteps('parallel-graviton');

                // Re-enable all test buttons
                this.enableAllTestButtons();

                // Re-enable all auto-retry buttons
                this.enableAutoRetryButton('unoptimized-graviton');
                this.enableAutoRetryButton('unoptimized-x86');

                // Clear all status displays
                const statusElements = document.querySelectorAll('.result-status');
                statusElements.forEach(el => {
                    el.textContent = '';
                    el.className = 'result-status';
                });

                // Remove all incomplete result cards (staging/running/failed/lost)
                const resultsContainer = document.getElementById('results-container');
                const resultCards = resultsContainer.querySelectorAll('.result-card');
                let removedCount = 0;

                resultCards.forEach(card => {
                    const statusElement = card.querySelector('.result-status');
                    if (statusElement) {
                        const statusText = statusElement.textContent.toLowerCase();
                        // Remove cards that are not completed
                        if (statusText.includes('staging') ||
                            statusText.includes('running') ||
                            statusText.includes('failed') ||
                            statusText.includes('lost') ||
                            statusText.includes('unknown')) {
                            card.remove();
                            removedCount++;
                        }
                    }
                });

                if (removedCount > 0) {
                    this.logCommunication(`🗑️ Removed ${removedCount} incomplete result card(s)`, 'info');
                }

                // If no cards remain, show placeholder
                if (resultsContainer.querySelectorAll('.result-card').length === 0) {
                    resultsContainer.innerHTML = '<p>No benchmark results yet. Run a benchmark to see performance data.</p>';
                }

                // Refresh status
                await this.checkSystemStatus();
            } else {
                throw new Error('Failed to stop benchmarks');
            }
        } catch (error) {
            console.error('Error stopping benchmarks:', error);
            this.logCommunication(`❌ Error stopping benchmarks: ${error.message}`, 'error');
        }
    }

    async startAutoRetryBuild(testType, instanceType, buildAttempts = 10) {
        console.log('=== startAutoRetryBuild called ===', testType, instanceType, 'attempts:', buildAttempts);

        // Auto-select "Build from source" for the appropriate test type
        if (testType === 'unoptimized-graviton') {
            const compileRadio = document.querySelector('input[name="graviton-build-mode"][value="compile"]');
            if (compileRadio) {
                compileRadio.checked = true;
                // Trigger change event to update button text
                compileRadio.dispatchEvent(new Event('change'));
            }
        } else if (testType === 'unoptimized-x86') {
            const compileRadio = document.querySelector('input[name="x86-build-mode"][value="compile"]');
            if (compileRadio) {
                compileRadio.checked = true;
                // Trigger change event to update button text
                compileRadio.dispatchEvent(new Event('change'));
            }
        }

        const confirmed = confirm(
            '🔄 Auto-Retry EC2 Staging Mode\n\n' +
            'This will automatically retry the EC2 staging/installation until it succeeds.\n' +
            'Build mode has been set to "Build from source" for optimization testing.\n' +
            'No images will be processed - this is for testing the build process only.\n\n' +
            'The system will:\n' +
            '• Attempt to install/build OpenCV\n' +
            '• If it fails, analyze the error logs\n' +
            '• Automatically retry with fixes\n' +
            `• Continue until success or max retries (${buildAttempts})\n\n` +
            'Do you want to proceed?'
        );

        console.log('=== Confirmation result:', confirmed);
        if (!confirmed) return;

        // Get the button element
        const buttonId = testType === 'unoptimized-graviton' ? 'auto-retry-graviton' : 'auto-retry-x86';
        const button = document.getElementById(buttonId);
        console.log('=== Button found:', button);

        // Disable button and change appearance
        if (button) {
            button.disabled = true;
            button.style.opacity = '0.5';
            button.style.cursor = 'not-allowed';
            button.textContent = '⏳ Auto-Retry Running...';
        }

        // Get build mode
        let buildMode = 'pip';
        if (testType === 'unoptimized-graviton') {
            const gravitonMode = document.querySelector('input[name="graviton-build-mode"]:checked');
            buildMode = gravitonMode ? gravitonMode.value : 'pip';
        } else if (testType === 'unoptimized-x86') {
            const x86Mode = document.querySelector('input[name="x86-build-mode"]:checked');
            buildMode = x86Mode ? x86Mode.value : 'pip';
        }
        console.log('=== Build mode:', buildMode);

        this.logCommunication(`🔄 Starting auto-retry build for ${testType} (${buildMode} mode)`, 'info');
        this.logCommunication(`Will retry up to ${buildAttempts} times until installation succeeds`, 'info');

        // Reset tracking variables for Claude analysis/fixes display
        this.lastShownAnalysis = null;
        this.lastShownFixes = null;

        const claudeApiKey = this.getClaudeApiKey();
        console.log('=== Claude API key:', claudeApiKey ? 'present' : 'missing');

        if (!claudeApiKey) {
            alert('⚠️ Claude API Key not configured!\n\nPlease add your Claude API key in the Configuration section above.');
            // Re-enable button
            if (button) {
                button.disabled = false;
                button.style.opacity = '1';
                button.style.cursor = 'pointer';
                button.textContent = '🔄 Auto-Retry EC2 Staging Until Success';
            }
            return;
        }

        console.log('=== About to make POST request...');

        try {
            const url = this.getApiUrl('/api/build/auto-retry');
            console.log('=== POST URL:', url);

            const payload = {
                testType,
                instanceType,
                buildMode,
                maxRetries: buildAttempts,
                claudeApiKey
            };
            console.log('=== POST payload:', payload);

            const response = await fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(payload)
            });

            console.log('=== Response received:', response.status, response.ok);

            if (!response.ok) {
                const errorText = await response.text();
                console.error('=== Response error:', errorText);
                throw new Error('Failed to start auto-retry build: ' + errorText);
            }

            const responseData = await response.json();
            console.log('=== Response data:', responseData);
            const { taskId } = responseData;

            this.logCommunication(`Auto-retry task started: ${taskId}`, 'success');

            // Poll for auto-retry progress
            console.log('=== Starting polling for task:', taskId);
            this.pollAutoRetryProgress(taskId, testType, buttonId);

        } catch (error) {
            console.error('=== ERROR in startAutoRetryBuild:', error);
            this.logCommunication(`Error: ${error.message}`, 'error');
            alert('Failed to start auto-retry build: ' + error.message);

            // Re-enable button on error
            if (button) {
                button.disabled = false;
                button.style.opacity = '1';
                button.style.cursor = 'pointer';
                button.textContent = '🔄 Auto-Retry EC2 Staging Until Success';
            }
        }
    }

    async pollAutoRetryProgress(taskId, testType, buttonId) {
        const button = document.getElementById(buttonId);

        const pollInterval = setInterval(async () => {
            try {
                const response = await fetch(this.getApiUrl(`/api/build/auto-retry/${taskId}/status`));
                const data = await response.json();

                if (data.status === 'running') {
                    // Use total elapsed time since auto-retry started
                    const totalElapsedMin = data.totalElapsedMinutes || 0;

                    this.logCommunication(
                        `🔄 Attempt ${data.attempt}/${data.maxRetries}: ${data.currentStep}`,
                        'info'
                    );

                    // Show Claude analysis if available (only once per attempt)
                    if (data.claudeAnalysis && !this.lastShownAnalysis) {
                        this.logCommunication(
                            `🤖 Claude AI: ${data.claudeAnalysis}`,
                            'info'
                        );
                        this.lastShownAnalysis = data.claudeAnalysis;
                    }

                    // Show Claude fixes if available (only once per attempt)
                    if (data.claudeFixes && data.claudeFixes.length > 0 && !this.lastShownFixes) {
                        this.logCommunication(
                            `🔧 Claude suggested ${data.claudeFixes.length} fixes:`,
                            'info'
                        );
                        data.claudeFixes.forEach((fix, idx) => {
                            this.logCommunication(`   ${idx + 1}. ${fix}`, 'info');
                        });
                        this.lastShownFixes = data.claudeFixes;
                    }

                    // Update button text with current attempt and TOTAL elapsed time
                    if (button) {
                        button.textContent = `⏳ Attempt ${data.attempt}/${data.maxRetries} (${totalElapsedMin} min total)`;
                    }

                    if (data.lastError || data.last_error) {
                        const errorMsg = data.lastError || data.last_error;
                        // Show a concise error message
                        if (errorMsg.length > 100) {
                            this.logCommunication(`Last error: ${errorMsg.substring(0, 100)}...`, 'error');
                        } else {
                            this.logCommunication(`Last error: ${errorMsg}`, 'error');
                        }
                    }
                } else if (data.status === 'success') {
                    clearInterval(pollInterval);

                    // Format total time
                    const totalTime = data.totalTimeMinutes !== undefined
                        ? `${data.totalTimeMinutes}m ${data.totalTimeSeconds}s`
                        : 'unknown';

                    this.logCommunication(
                        `✅ Build succeeded after ${data.attempt} attempt(s)!`,
                        'success'
                    );
                    this.logCommunication(
                        `⏱️ Total time: ${totalTime}`,
                        'success'
                    );
                    this.playNotificationSound('success');

                    // Re-enable button with success state
                    if (button) {
                        button.disabled = false;
                        button.style.opacity = '1';
                        button.style.cursor = 'pointer';
                        button.textContent = '✅ Auto-Retry EC2 Staging Until Success';

                        // Reset button text after 3 seconds
                        setTimeout(() => {
                            button.textContent = '🔄 Auto-Retry EC2 Staging Until Success';
                        }, 3000);
                    }
                } else if (data.status === 'failed') {
                    clearInterval(pollInterval);

                    // Format total time
                    const totalTime = data.total_time_minutes !== undefined
                        ? `${data.total_time_minutes}m ${data.total_time_seconds}s`
                        : 'unknown';

                    this.logCommunication(
                        `❌ Build failed after ${data.max_retries || data.maxRetries || 'unknown'} attempts`,
                        'error'
                    );
                    this.logCommunication(
                        `⏱️ Total time: ${totalTime}`,
                        'error'
                    );

                    // Show detailed error message
                    const finalError = data.error || data.last_error || 'Unknown error';
                    if (finalError.length > 150) {
                        this.logCommunication(`Final error: ${finalError.substring(0, 150)}...`, 'error');
                        this.logCommunication(`💡 Check browser console or orchestrator logs for full error details`, 'info');
                    } else {
                        this.logCommunication(`Final error: ${finalError}`, 'error');
                    }
                    this.playNotificationSound('error');

                    // Re-enable button with error state
                    if (button) {
                        button.disabled = false;
                        button.style.opacity = '1';
                        button.style.cursor = 'pointer';
                        button.textContent = '❌ Auto-Retry EC2 Staging Until Success';

                        // Reset button text after 3 seconds
                        setTimeout(() => {
                            button.textContent = '🔄 Auto-Retry EC2 Staging Until Success';
                        }, 3000);
                    }
                }

            } catch (error) {
                console.error('Error polling auto-retry progress:', error);
                // Don't clear interval on network errors, keep trying
            }
        }, 3000);
    }

    saveConfiguration() {
        const claudeApiKey = document.getElementById('claude-api-key').value.trim();
        const marketplaceAmiId = document.getElementById('marketplace-ami-id').value.trim();

        const config = {
            claudeApiKey,
            marketplaceAmiId
        };

        // Save to localStorage
        localStorage.setItem('opencvBenchmarkConfig', JSON.stringify(config));

        // Also send to backend
        fetch(this.getApiUrl('/api/config/save'), {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(config)
        }).then(response => {
            if (response.ok) {
                this.logCommunication('✅ Configuration saved successfully', 'success');
                alert('Configuration saved!');
            } else {
                throw new Error('Failed to save configuration');
            }
        }).catch(error => {
            console.error('Error saving configuration:', error);
            this.logCommunication('⚠️ Configuration saved locally only (backend unavailable)', 'warning');
            alert('Configuration saved locally. Backend may be offline.');
        });
    }

    loadConfiguration() {
        // Load from localStorage
        const savedConfig = localStorage.getItem('opencvBenchmarkConfig');
        if (savedConfig) {
            try {
                const config = JSON.parse(savedConfig);

                if (config.claudeApiKey) {
                    document.getElementById('claude-api-key').value = config.claudeApiKey;
                }
                if (config.marketplaceAmiId) {
                    document.getElementById('marketplace-ami-id').value = config.marketplaceAmiId;
                    // Also update option 1 field
                    const option1Field = document.getElementById('marketplace-ami-id-option1');
                    if (option1Field) {
                        option1Field.value = config.marketplaceAmiId;
                    }
                }

                console.log('Configuration loaded from localStorage');
            } catch (error) {
                console.error('Error loading configuration:', error);
            }
        }
    }

    clearResults() {
        // Confirm before clearing
        if (!confirm('Clear all benchmark results and processed images?')) {
            return;
        }

        // Clear benchmark results
        const resultsContainer = document.getElementById('results-container');
        resultsContainer.innerHTML = '<p>No benchmark results yet. Run a benchmark to see performance data.</p>';

        // Clear processed images
        const imageGallery = document.getElementById('image-gallery');
        imageGallery.innerHTML = '<p>Processed images will appear here after running benchmarks.</p>';

        // Clear test results array
        this.testResults = [];

        // Log the action
        this.logCommunication('🗑️ Cleared all benchmark results and processed images', 'info');

        console.log('Benchmark results and processed images cleared');
    }

    getClaudeApiKey() {
        return document.getElementById('claude-api-key').value.trim();
    }

    getMarketplaceAmiId() {
        return document.getElementById('marketplace-ami-id').value.trim();
    }

    getMarketplaceLicenseKey() {
        return document.getElementById('marketplace-license-key').value.trim();
    }
}

// Initialize the application when DOM is loaded
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        new OpenCVBenchmarkApp();
    });
} else {
    // DOM is already loaded (script loaded dynamically)
    new OpenCVBenchmarkApp();
}