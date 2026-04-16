// Add this to pollBenchmarkProgress function after line 813

let benchmarkStartTime = Date.now();
let lastElapsedLogTime = 0;

// Then in the pollInterval, after console.log, add:

// Calculate elapsed time
const elapsedSeconds = Math.floor((Date.now() - benchmarkStartTime) / 1000);
const elapsedMinutes = Math.floor(elapsedSeconds / 60);
const remainingSeconds = elapsedSeconds % 60;
const elapsedDisplay = `${elapsedMinutes}:${remainingSeconds.toString().padStart(2, '0')}`;

// Then update the log messages to include (elapsed: ${elapsedDisplay})

// And add this before the completed/failed checks:

// Show periodic elapsed time updates (every 15 seconds) when running
if (data.status === 'running' && elapsedSeconds - lastElapsedLogTime >= 15) {
    this.logCommunication(`⏱️ Elapsed time: ${elapsedDisplay} - ${lastBuildMessage || 'In progress...'}`, 'info');
    lastElapsedLogTime = elapsedSeconds;
}
