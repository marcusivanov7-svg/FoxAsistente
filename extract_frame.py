import cv2
cap = cv2.VideoCapture(r"C:\Users\Matias Lopez\Videos\NVIDIA\Desktop\Desktop 2026.09.22 - 17.09.32.02.mp4")
cap.set(cv2.CAP_PROP_POS_MSEC, 19000)      # Frame at 19 seconds
ret, frame = cap.read()
if ret:
    cv2.imwrite(r"C:\Users\Matias Lopez\.gemini\antigravity\brain\27d5bb26-d756-462e-a185-c53ce6ec074c\.user_uploaded\media_1790106331031.png", frame)
    print("Frame extracted to artifact directory")
else:
    print("Failed to extract frame")
cap.release()
