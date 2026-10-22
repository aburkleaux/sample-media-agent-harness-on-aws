/**
 * @vitest-environment jsdom
 */
import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import ImageAnnotated from './ImageAnnotated';
import { theme } from '../../../styles/tokens';

const BOXES = [
  { x: 30.488, y: 17.364, w: 14.083, h: 35.581, label: 'Male' },
  { x: 60, y: 20, w: 10, h: 20, label: 'Female' },
];

function view(data: Record<string, unknown>) {
  return render(<ImageAnnotated data={data as never} title="Faces" theme={theme} />);
}

describe('ImageAnnotated', () => {
  it('places boxes using percentage coordinates', () => {
    const { container } = view({ src: 'https://x/f.jpg', annotations: BOXES });
    const boxes = Array.from(container.querySelectorAll('div[style*="position: absolute"]'));
    expect(boxes.length).toBe(2);
    expect((boxes[0] as HTMLElement).style.left).toBe('30.488%');
    expect((boxes[0] as HTMLElement).style.width).toBe('14.083%');
  });

  it('caps a tall still at 62vh and keeps the boxes on the image, not the canvas', () => {
    view({ src: 'https://x/vertical.jpg', annotations: BOXES });
    const frame = screen.getByTestId('image-annotated-frame');
    const img = frame.querySelector('img') as HTMLImageElement;
    expect(img.style.maxHeight).toBe('62vh');
    expect(img.style.width).toBe('auto'); // not stretched to the canvas width
    expect(frame.style.alignSelf).toBe('center'); // the frame shrink-wraps the image
    expect(frame.querySelectorAll('div[style*="position: absolute"]').length).toBe(2);
  });

  it('shows the chosen still and its detection labels', () => {
    view({ src: 'https://x/f.jpg', annotations: BOXES });
    expect(screen.getAllByText('Male').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Female').length).toBeGreaterThan(0);
  });

  it('offers a frame gallery when several stills carried detections', () => {
    view({
      src: 'https://x/a.jpg', annotations: BOXES,
      images: [
        { src: 'https://x/a.jpg', label: 'frame_0000900.jpg', detections: 2 },
        { src: 'https://x/b.jpg', label: 'frame_0001798.jpg', detections: 1 },
      ],
    });
    expect(screen.getByRole('group', { name: /2 analysed frames/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /frame_0001798\.jpg/ })).toBeTruthy();
  });

  it('switches the displayed still when a gallery entry is clicked', () => {
    const { container } = view({
      src: 'https://x/a.jpg', annotations: BOXES,
      images: [
        { src: 'https://x/a.jpg', label: 'a', detections: 2 },
        { src: 'https://x/b.jpg', label: 'b', detections: 1, annotations: [{ x: 1, y: 2, w: 3, h: 4, label: 'other' }] },
      ],
    });
    fireEvent.click(screen.getByRole('button', { name: /^b$/ }));
    expect((container.querySelector('img') as HTMLImageElement).src).toContain('/b.jpg');
    expect(screen.getAllByText('other').length).toBeGreaterThan(0);
  });

  it('hides the gallery when there is only one still', () => {
    view({ src: 'https://x/a.jpg', annotations: BOXES, images: [{ src: 'https://x/a.jpg', label: 'a' }] });
    expect(screen.queryByRole('group', { name: /analysed frames/ })).toBeNull();
  });

  it('emits a select interaction with the clicked image url (drives copy-source-url)', () => {
    const events: Array<{ type: string; data?: unknown }> = [];
    render(<ImageAnnotated
      data={{ src: 'https://x/a.jpg', annotations: BOXES, images: [
        { src: 'https://cdn/a.png', label: 'a' },
        { src: 'https://cdn/b.png', label: 'b' },
      ] } as never}
      title="G" theme={theme}
      onInteraction={(e) => events.push(e)} />);
    fireEvent.click(screen.getByRole('button', { name: /^b$/ }));
    const sel = events.find(e => e.type === 'select');
    expect(sel).toBeTruthy();
    expect((sel!.data as { url: string }).url).toBe('https://cdn/b.png');
  });

  it('renders findings that are not spatial, such as who was identified', () => {
    view({
      src: 'https://x/f.jpg', annotations: BOXES,
      tables: [{
        columns: [{ key: 'name', label: 'Name' }, { key: 'age_range', label: 'Age Range' }],
        rows: [{ name: 'Kawhi Leonard', age_range: '28–36' }],
      }],
    });
    expect(screen.getByText('Kawhi Leonard')).toBeTruthy();
    expect(screen.getByText('28–36')).toBeTruthy();
  });

  it('surfaces summary metadata', () => {
    view({ src: 'https://x/f.jpg', annotations: BOXES, metadata: { num_people: 2, faces_detected: 4 } });
    expect(screen.getByText(/num people/)).toBeTruthy();
    expect(screen.getByText('2')).toBeTruthy();
  });

  it('renders without boxes rather than crashing', () => {
    const { container } = view({ src: 'https://x/f.jpg', annotations: [] });
    expect(container.querySelector('img')).toBeTruthy();
  });

  it('tolerates a missing annotations array', () => {
    const { container } = view({ src: 'https://x/f.jpg' });
    expect(container.querySelector('img')).toBeTruthy();
  });
});
