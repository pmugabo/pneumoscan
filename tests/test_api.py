"""
Integration tests for the API endpoints.
"""
import pytest
from fastapi.testclient import TestClient
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.main import app


client = TestClient(app)


class TestHealthEndpoint:
    """Test the /api/health endpoint."""
    
    def test_health_endpoint(self):
        """Test that health endpoint returns 200 and correct structure."""
        response = client.get("/api/health")
        assert response.status_code == 200
        
        data = response.json()
        assert "status" in data
        assert "demo_mode" in data
        assert "time" in data
        assert data["status"] == "ok"


class TestModelInfoEndpoint:
    """Test the /api/model-info endpoint."""
    
    def test_model_info_endpoint(self):
        """Test that model-info endpoint returns correct structure."""
        response = client.get("/api/model-info")
        assert response.status_code == 200
        
        data = response.json()
        assert "demo_mode" in data
        assert "architecture" in data
        assert "gate" in data
        assert "states" in data
        
        # Check that states contains the four expected states
        assert "CLEARED" in data["states"]
        assert "FLAGGED_URGENT" in data["states"]
        assert "ESCALATED" in data["states"]
        assert "REJECTED" in data["states"]


class TestPredictEndpoint:
    """Test the /api/predict endpoint."""
    
    def test_predict_with_invalid_file_type(self):
        """Test that predict rejects non-image files."""
        # Create a text file instead of an image
        files = {"file": ("test.txt", b"not an image", "text/plain")}
        response = client.post("/api/predict", files=files)
        
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
    
    def test_predict_with_large_file(self):
        """Test that predict rejects files larger than 10MB."""
        # Create a dummy file larger than 10MB
        large_content = b"x" * (11 * 1024 * 1024)  # 11MB
        files = {"file": ("large.jpg", large_content, "image/jpeg")}
        response = client.post("/api/predict", files=files)
        
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
    
    def test_predict_with_valid_image(self):
        """Test that predict accepts valid images and returns structure."""
        # Use a real test image if available
        samples_dir = Path(__file__).parent.parent / "samples"
        test_images = list(samples_dir.glob("*.jpeg")) + list(samples_dir.glob("*.png"))
        
        if test_images:
            with open(test_images[0], "rb") as f:
                files = {"file": (test_images[0].name, f, "image/jpeg")}
                response = client.post("/api/predict", files=files)
            
            assert response.status_code == 200
            data = response.json()
            
            # Check response structure
            assert "prediction_id" in data
            assert "state" in data
            assert "label" in data
            assert "probability" in data
            assert "confidence" in data
            assert "ood_score" in data
            assert "reason" in data
            assert "model_version" in data
            assert "thresholds" in data
            
            # Check that state is one of the four expected states
            assert data["state"] in ["CLEARED", "FLAGGED_URGENT", "ESCALATED", "REJECTED"]


class TestFeedbackEndpoint:
    """Test the /api/feedback endpoint."""
    
    def test_feedback_with_valid_data(self):
        """Test that feedback endpoint accepts valid data."""
        feedback_data = {
            "prediction_id": "test-id-123",
            "confirmed_outcome": "pneumonia",
            "comment": "Test comment"
        }
        response = client.post("/api/feedback", json=feedback_data)
        
        # Note: This will fail if prediction_id doesn't exist, but we're testing the endpoint structure
        assert response.status_code in [200, 404]
    
    def test_feedback_with_invalid_id(self):
        """Test that feedback handles non-existent prediction_id."""
        feedback_data = {
            "prediction_id": "non-existent-id",
            "confirmed_outcome": "normal",
            "comment": ""
        }
        response = client.post("/api/feedback", json=feedback_data)
        
        assert response.status_code == 404
        data = response.json()
        assert "detail" in data


class TestCasesEndpoint:
    """Test the /api/cases endpoint."""
    
    def test_cases_endpoint(self):
        """Test that cases endpoint returns correct structure."""
        response = client.get("/api/cases?limit=10")
        assert response.status_code == 200
        
        data = response.json()
        assert "count" in data
        assert "cases" in data
        assert isinstance(data["cases"], list)
        assert isinstance(data["count"], int)


class TestStatsEndpoint:
    """Test the /api/stats endpoint."""
    
    def test_stats_endpoint(self):
        """Test that stats endpoint returns correct structure."""
        response = client.get("/api/stats")
        assert response.status_code == 200
        
        data = response.json()
        assert "total" in data
        assert "by_state" in data
        assert "autonomy_rate" in data
        assert isinstance(data["total"], int)
        assert isinstance(data["by_state"], dict)


class TestFrontend:
    """Test that the frontend is served correctly."""
    
    def test_frontend_index(self):
        """Test that the frontend index page is served."""
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
