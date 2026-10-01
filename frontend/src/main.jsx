import ReactDOM from 'react-dom/client';
import App from './App';
// No StrictMode double replay of imperative management setup in the shipped bundle.
ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
