"""
Unit tests for the inference engine.
"""
import pytest
from pathlib import Path
from PIL import Image
import numpy as np

# Import the engine from the backend
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.inference import Engine, TriageResult


class TestEngine:
    """Test the Engine class and its methods."""
    
    def test_engine_initialization(self):
        """Test that the engine initializes correctly."""
        engine = Engine()
        assert engine is not None
        assert hasattr(engine, 'demo')
        assert hasattr(engine, 'model')
    
    def test_demo_mode_when_no_model(self):
        """Test that engine enters demo mode when model files are missing."""
        engine = Engine(model_dir=Path("/nonexistent/path"))
        assert engine.demo is True
    
    def test_mahalanobis_distance_shape(self):
        """Test that Mahalanobis distance returns a float."""
        engine = Engine()
        if not engine.demo and engine.means is not None:
            feat = np.random.randn(1280).astype(np.float64)
            distance = engine.mahalanobis(feat)
            assert isinstance(distance, float)
            assert distance >= 0
    
    def test_preprocess_output_shape(self):
        """Test that preprocessing returns correct shape."""
        engine = Engine()
        # Create a dummy image
        img = Image.new('RGB', (300, 400), color='white')
        rgb, batch = engine.preprocess(img)
        
        # Check that rgb is a PIL Image
        assert isinstance(rgb, Image.Image)
        assert rgb.size == (224, 224)
        
        # Check that batch has correct shape (1, 3, 224, 224)
        assert batch.shape == (1, 3, 224, 224)
    
    def test_predict_in_demo_mode(self):
        """Test that predict returns demo result when in demo mode."""
        engine = Engine(model_dir=Path("/nonexistent/path"))
        img = Image.new('RGB', (224, 224), color='white')
        result = engine.predict(img)
        
        assert isinstance(result, TriageResult)
        assert result.demo_mode is True
        assert result.state == "ESCALATED"
        assert result.label == "unavailable"
    
    def test_info_method(self):
        """Test that info() returns correct structure."""
        engine = Engine()
        info = engine.info()
        
        assert isinstance(info, dict)
        assert 'demo_mode' in info
        assert 'architecture' in info
        assert 'gate' in info
        assert 'states' in info


class TestTriageResult:
    """Test the TriageResult dataclass."""
    
    def test_triage_result_creation(self):
        """Test that TriageResult can be created with valid data."""
        result = TriageResult(
            state="CLEARED",
            label="Normal",
            probability=0.2,
            confidence=0.8,
            ood_score=100.0,
            reason="Test reason",
            heatmap_png=None,
            model_version="test",
            thresholds={"confidence": 0.975, "distance": 2849.0, "temperature": 1.293},
            demo_mode=False
        )
        
        assert result.state == "CLEARED"
        assert result.label == "Normal"
        assert result.probability == 0.2
        assert result.confidence == 0.8
    
    def test_triage_result_to_dict(self):
        """Test that to_dict() returns a dictionary."""
        result = TriageResult(
            state="CLEARED",
            label="Normal",
            probability=0.2,
            confidence=0.8,
            ood_score=100.0,
            reason="Test reason",
            heatmap_png=None,
            model_version="test",
            thresholds={"confidence": 0.975},
            demo_mode=False
        )
        
        result_dict = result.to_dict()
        assert isinstance(result_dict, dict)
        assert result_dict['state'] == "CLEARED"
        assert result_dict['label'] == "Normal"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
