# Contributing to HYDRA-UMC-ANOMALY-DETECTOR 🦾

We welcome contributions to the AI-driven predictive maintenance engine of the HYDRA-UMC platform.

## Technology Stack
- **Language**: Python 3.12.
- **AI Frameworks**: PyTorch, TensorFlow Lite (for Edge inference).
- **DSP**: NumPy, SciPy (FFT, filtering).
- **Visualization**: Matplotlib, Plotly.

## Guidelines
1. **Signal Processing Accuracy**: Ensure that all FFT and spectral analysis logic is validated against standard signal processing benchmarks.
2. **Model Generalization**: Predictive models must be trained on diverse motor load scenarios to prevent false positives during high-speed movements.
3. **Real-time Performance**: Anomaly detection inference should not exceed 50ms to allow for near-instant operator alerting.
4. **Data Privacy**: Ensure that telemetry used for training is anonymized and adheres to the ecosystem's data handling policies.
