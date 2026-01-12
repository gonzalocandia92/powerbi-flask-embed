# UI Standardization and SweetAlert2 Integration

## Overview

This document describes the comprehensive UI standardization and SweetAlert2 integration implemented across the PowerBI Flask Embed application.

## Design System

### Color Palette

**Primary Colors:**
- Purple Gradient: `#667eea` to `#764ba2`
- Dark Gradient: `#2c3e50` to `#3498db`
- Success: `#28a745`
- Warning: `#f39c12`
- Danger: `#d33` / `#dc3545`
- Info: `#3085d6`

### Typography

- **Headers**: Bold font-weight (600-700)
- **Body**: Regular font-weight (400-500)
- **Code**: `SFMono-Regular`, `Consolas`, `Liberation Mono`

### Component Styling

#### Cards
- Border-radius: `12px`
- Box-shadow: `0 2px 8px rgba(0,0,0,0.08)`
- Header: Purple gradient with white text
- Body: White background with 1.5rem padding
- Footer: Light gradient background

#### Tables
- Header: Dark gradient (`#2c3e50` to `#3498db`) with white text
- Row hover: Light purple background with left border accent
- Smooth transitions on all interactions
- Border-left highlight on hover: `#667eea`

#### Buttons
- Border-radius: `8px`
- Font-weight: `500`
- Hover: Translate up 2px with shadow
- Primary: Purple gradient
- Outline variants for secondary actions

#### Badges
- Border-radius: `6px`
- Padding: `0.4em 0.8em`
- Font-weight: `500`

#### Code Blocks
- Background: `rgba(102, 126, 234, 0.1)`
- Border: `1px solid rgba(102, 126, 234, 0.2)`
- Color: `#667eea`
- Border-radius: `4px`

## SweetAlert2 Integration

### Global Configuration

The application includes global SweetAlert2 configuration in `layout.html`:

```javascript
// Toast notifications for non-blocking messages
const Toast = Swal.mixin({
  toast: true,
  position: 'top-end',
  showConfirmButton: false,
  timer: 3000,
  timerProgressBar: true
});

// Helper functions
function confirmDelete(message, title, confirmText)
function showSuccess(message)
function showError(message)
function showInfo(message)
```

### Confirmation Dialogs

All confirmation dialogs follow this pattern:

```javascript
Swal.fire({
  title: '¿Está seguro?',
  text: 'Message describing the action',
  icon: 'warning',
  showCancelButton: true,
  confirmButtonColor: '#d33',
  cancelButtonColor: '#3085d6',
  confirmButtonText: 'Sí, eliminar',
  cancelButtonText: 'Cancelar',
  reverseButtons: true,
  focusCancel: true
})
```

### Types of Confirmations

1. **Delete Confirmations** (Red button)
   - Icon: `warning`
   - Confirm color: `#d33`
   - Used for: Entity deletion, link deletion

2. **Regenerate Credentials** (Orange button)
   - Icon: `warning`
   - Confirm color: `#f39c12`
   - Used for: Credential regeneration

3. **Success Notifications** (Green toast)
   - Icon: `success`
   - Position: `top-end`
   - Timer: 3 seconds

4. **Error Notifications** (Red toast)
   - Icon: `error`
   - Position: `top-end`
   - Timer: 3 seconds

## Templates Updated

### Base Templates
- `layout.html` - Added SweetAlert2 library and global styling
- `base_list.html` - Standardized list template with SweetAlert2
- `base_detail.html` - New base template for detail pages

### Entity Detail Pages
- `tenants/detail.html`
- `clients/detail.html`
- `workspaces/detail.html`
- `reports/detail.html`
- `usuarios_pbi/detail.html`

### Other Templates
- `index.html` - Dashboard with URL management
- `configs/detail.html` - Configuration details
- `admin/empresas/list.html` - Empresas listing
- `admin/empresas/detail.html` - Empresa details

## User Experience Improvements

### Visual Feedback
- Hover effects on all interactive elements
- Smooth transitions (0.2s - 0.3s)
- Color changes on state
- Shadow effects on elevation

### Safety Features
- Cancel button focused by default
- Confirmation required for destructive actions
- Clear visual distinction between safe and dangerous actions
- Informative messages in confirmations

### Accessibility
- High contrast text colors
- Clear button labels
- Keyboard navigation support
- Focus indicators

## Migration Guide

### For Developers

When creating new templates:

1. **List Pages**: Extend `base_list.html`
2. **Detail Pages**: Extend `base_detail.html`
3. **Confirmations**: Use SweetAlert2 instead of native `confirm()`

Example delete button:
```html
<button type="button" 
        class="btn btn-outline-danger delete-btn"
        data-action="{{ url_for('entity.delete', id=item.id) }}"
        data-item="entity name">
  <i class="bi bi-trash"></i>
</button>
```

Example JavaScript:
```javascript
document.querySelectorAll('.delete-btn').forEach(button => {
  button.addEventListener('click', function() {
    const action = this.dataset.action;
    const item = this.dataset.item;
    
    Swal.fire({
      title: '¿Está seguro?',
      text: `¿Desea eliminar este ${item}?`,
      icon: 'warning',
      showCancelButton: true,
      confirmButtonColor: '#d33',
      cancelButtonColor: '#3085d6',
      confirmButtonText: 'Sí, eliminar',
      cancelButtonText: 'Cancelar'
    }).then((result) => {
      if (result.isConfirmed) {
        const form = document.createElement('form');
        form.method = 'POST';
        form.action = action;
        document.body.appendChild(form);
        form.submit();
      }
    });
  });
});
```

## Browser Compatibility

- Modern browsers (Chrome, Firefox, Safari, Edge)
- SweetAlert2 requires ES6 support
- Graceful degradation for older browsers

## Performance

- SweetAlert2 loaded from CDN (cached)
- CSS animations use GPU acceleration
- Minimal JavaScript overhead
- No jQuery dependency

## Future Enhancements

Potential improvements:
- Dark mode support
- More animation options
- Custom SweetAlert2 themes
- Loading states for async operations
- Form validation with SweetAlert2

## References

- [SweetAlert2 Documentation](https://sweetalert2.github.io/)
- [Bootstrap 5.3 Documentation](https://getbootstrap.com/docs/5.3/)
- [Bootstrap Icons](https://icons.getbootstrap.com/)
